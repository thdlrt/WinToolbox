"""Manual settings-only snapshots. Business data and WebDAV identity never restore."""
import contextlib
import hashlib
import json
import math
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from .features.backups import MAGIC, encrypt, decrypt
from .settings import atomic_json

CONFIG_FILES = {'settings.json', 'orb-settings.json', 'memory-cleaner.json', 'fnconnect-tun.json'}
MAX_CONFIG_BYTES = 16 * 1024 * 1024


def validate_config(name, value):
    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or key in ('api_key', 'password', 'password_dpapi', 'token', 'access_token'):
                    raise ValueError('普通配置中不能包含明文凭据')
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, float) and not math.isfinite(node):
            raise ValueError('配置包含无效数值')
    if not isinstance(value, dict):
        raise ValueError('配置字段必须是对象')
    walk(value)
    if name == 'settings.json':
        if any(key in value and not isinstance(value[key], dict) for key in ('roles', 'preferences')):
            raise ValueError('模型角色和偏好设置格式无效')
        if 'presets' in value and (not isinstance(value['presets'], list) or any(not isinstance(preset, dict) for preset in value['presets'])):
            raise ValueError('预设格式无效')
        providers = value.get('providers', [])
        if not isinstance(providers, list):
            raise ValueError('模型供应商格式无效')
        ids = set()
        for provider in providers:
            if not isinstance(provider, dict) or not isinstance(provider.get('id'), str) or not provider['id'].strip() or provider['id'] in ids or provider.get('kind') not in ('openai', 'dashscope', 'gemini'):
                raise ValueError('模型供应商 ID 或类型无效')
            if set(provider) - {'id', 'name', 'kind', 'base_url', 'region'}:
                raise ValueError('模型供应商包含不支持的配置字段')
            if any(key in provider and not isinstance(provider[key], str) for key in ('name', 'base_url', 'region')):
                raise ValueError('模型供应商字段格式无效')
            ids.add(provider['id'])
        for role in value.get('roles', {}).values():
            if not isinstance(role, dict) or any(key in role and not isinstance(role[key], str) for key in ('provider_id', 'model')):
                raise ValueError('模型角色格式无效')
    elif name == 'orb-settings.json':
        from .features.orb_settings import OrbSettings
        OrbSettings.validate(value.get('actions'))
    elif name == 'memory-cleaner.json' and value.get('mode') not in ('default', 'full'):
        raise ValueError('内存清理配置格式无效')
    elif name == 'fnconnect-tun.json' and type(value.get('enabled')) is not bool:
        raise ValueError('VPN 配置格式无效')


def export_config(app, destination, job, password='', include_secrets=True):
    if include_secrets and len(password) < 8:
        raise ValueError('包含 API 密钥时，请填写至少 8 位备份密码')
    destination = Path(destination)
    payloads = {}
    for name in sorted(CONFIG_FILES):
        path = app.data_dir / name
        if path.is_file() and not path.is_symlink():
            value = json.loads(path.read_text('utf-8'))
            validate_config(name, value)
            payloads[name] = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if 'settings.json' not in payloads:
        value = dict(app.settings.value)
        validate_config('settings.json', value)
        payloads['settings.json'] = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if include_secrets:
        payloads['portable-secrets.json'] = json.dumps(app.settings.export_secrets(), ensure_ascii=False).encode('utf-8')
    manifest = {'format': 'WinToolbox configuration', 'version': 1, 'created_at': time.time(),
                'include_secrets': include_secrets, 'files': {name: {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()} for name, body in payloads.items()}}
    with tempfile.TemporaryDirectory(prefix='.config-export-', dir=destination.parent) as directory:
        archive_path = Path(directory) / 'config.zip'
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('backup-manifest.json', json.dumps(manifest, ensure_ascii=False))
            for name, body in payloads.items():
                job.check_cancelled()
                archive.writestr(name, body)
        if password:
            encrypt(archive_path, destination, password, job)
        else:
            with destination.open('xb') as output, archive_path.open('rb') as source:
                shutil.copyfileobj(source, output)
    return {'files': len(payloads), 'warnings': [], 'scope': 'config'}


def import_config(app, source, job, password=''):
    source = Path(source)
    with tempfile.TemporaryDirectory(prefix='.config-restore-', dir=app.data_dir.parent) as directory:
        with source.open('rb') as stream:
            encrypted = stream.read(8) == MAGIC
        archive_path = source
        if encrypted:
            if not password:
                raise ValueError('配置备份需要密码')
            archive_path = Path(directory) / 'decrypted.zip'
            decrypt(source, archive_path, password, job)
        values, secrets = {}, None
        with zipfile.ZipFile(archive_path) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError('备份中有重复文件名')
            if archive.getinfo('backup-manifest.json').file_size > MAX_CONFIG_BYTES:
                raise ValueError('配置备份清单过大')
            manifest = json.loads(archive.read('backup-manifest.json'))
            if manifest.get('format') not in ('WinToolbox configuration', 'WinToolbox backup') or manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
                raise ValueError('不支持的配置备份格式')
            allowed = CONFIG_FILES | {'portable-secrets.json'}
            for name, metadata in manifest['files'].items():
                # Legacy full backups are intentionally projected to config only.
                if name not in allowed:
                    continue
                info = archive.getinfo(name)
                if info.file_size > MAX_CONFIG_BYTES or info.file_size != metadata.get('size'):
                    raise ValueError('配置备份文件大小校验失败')
                body = archive.read(name)
                if hashlib.sha256(body).hexdigest() != metadata.get('sha256'):
                    raise ValueError('配置备份哈希校验失败')
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError('配置备份字段格式无效')
                if name == 'portable-secrets.json':
                    if not encrypted or not manifest.get('include_secrets') or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                        raise ValueError('拒绝从未加密备份恢复 API 密钥')
                    secrets = value
                else:
                    validate_config(name, value)
                    values[name] = value
        if not values:
            raise ValueError('备份中没有可恢复的配置')
        recovery = app.data_dir.parent / (app.data_dir.name + '-recovery') / ('config-' + uuid.uuid4().hex)
        recovery.mkdir(parents=True)
        targets = set(values) | ({'secrets.json'} if secrets is not None else set())
        existed = set()
        with getattr(app, 'data_lock', contextlib.nullcontext()):
            for name in targets:
                target = app.data_dir / name
                if target.exists():
                    shutil.copy2(target, recovery / name)
                    existed.add(name)
            try:
                job.check_cancelled()
                for name, value in values.items():
                    atomic_json(app.data_dir / name, value)
                app.settings.reload()
                if secrets is not None:
                    app.settings.import_secrets(secrets)
                if hasattr(app, 'reset_local_llm'):
                    app.reset_local_llm()
                if hasattr(job, 'mark_committed'):
                    job.mark_committed()
            except BaseException:
                for name in targets:
                    if name in existed:
                        shutil.copy2(recovery / name, app.data_dir / name)
                    else:
                        (app.data_dir / name).unlink(missing_ok=True)
                app.settings.reload()
                raise
        return {'files': len(values), 'scope': 'config', 'recovery_path': str(recovery), 'include_secrets': secrets is not None}
