"""Versioned, encrypted cross-device configuration snapshots; never application data."""
import base64
import copy
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import ai_config
from .config_backup import CONFIG_FILES, validate_config
from .settings import atomic_json

FORMAT = 'wintoolbox-config-backup'
AAD = b'wintoolbox-config-backup-v2'
LIMIT = 4 * 1024 * 1024
DIRECTORY = 'config-backups/shared'


def encode(value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(body) > LIMIT:
        raise ValueError('配置备份超过 4 MiB 限制')
    return body


def parse(body):
    if len(body) > LIMIT:
        raise ValueError('配置备份过大')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('配置备份包含重复字段')
            result[key] = value
        return result
    return json.loads(body, object_pairs_hook=pairs)


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'format', 'version', 'platform', 'created_at', 'config', 'ai'}
            or value['format'] != FORMAT or type(value['version']) is not int or value['version'] != 2
            or value['platform'] not in ('windows', 'android') or not isinstance(value['config'], dict)
            or type(value['created_at']) not in (int, float) or not math.isfinite(value['created_at']) or value['created_at'] < 0):
        raise ValueError('配置备份版本或字段无效')
    ai_config.validate(value['ai'])
    if value['platform'] == 'windows':
        if set(value['config']) - CONFIG_FILES:
            raise ValueError('配置备份包含非配置文件')
        for name, item in value['config'].items():
            validate_config(name, item)
    encode(value)
    return copy.deepcopy(value)


def encrypt(value, password, *, salt=None, nonce=None):
    value = validate(value)
    if not isinstance(password, str) or not password:
        raise ValueError('请先保存 WebDAV 密码')
    salt = os.urandom(16) if salt is None else salt
    nonce = os.urandom(12) if nonce is None else nonce
    if len(salt) != 16 or len(nonce) != 12:
        raise ValueError('配置加密参数无效')
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200000, 32)
    cipher = AESGCM(key).encrypt(nonce, encode(value), AAD)
    envelope = {'format': FORMAT + '-encrypted', 'version': 2, 'salt': base64.b64encode(salt).decode(),
                'nonce': base64.b64encode(nonce).decode(), 'ciphertext': base64.b64encode(cipher).decode()}
    encode(envelope)
    return envelope


def decrypt(envelope, password):
    encode(envelope)
    if (not isinstance(envelope, dict) or set(envelope) != {'format', 'version', 'salt', 'nonce', 'ciphertext'}
            or envelope['format'] != FORMAT + '-encrypted' or type(envelope['version']) is not int or envelope['version'] != 2):
        raise ValueError('配置备份加密格式无效')
    try:
        salt, nonce, cipher = [base64.b64decode(envelope[k], validate=True) for k in ('salt', 'nonce', 'ciphertext')]
        if len(salt) != 16 or len(nonce) != 12 or len(cipher) < 16 or not isinstance(password, str) or not password:
            raise ValueError()
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200000, 32)
        body = AESGCM(key).decrypt(nonce, cipher, AAD)
    except Exception:
        raise ValueError('配置解密失败：WebDAV 密码不匹配或备份已损坏，本机配置未更改') from None
    return validate(parse(body))


def snapshot(app):
    with app.settings.lock:
        values = {}
        for name in CONFIG_FILES:
            path = app.data_dir / name
            if path.is_symlink():
                raise ValueError('配置文件不能是符号链接')
            if path.is_file():
                values[name] = parse(path.read_bytes())
        values['settings.json'] = copy.deepcopy(app.settings.value)
        return validate({'format': FORMAT, 'version': 2, 'platform': 'windows', 'created_at': time.time(),
                         'config': values, 'ai': ai_config.local_payload(app.settings)})


def revision(app):
    """Includes encrypted secret store and all restore targets, without returning them."""
    digest = hashlib.sha256()
    for name in sorted(CONFIG_FILES | {'secrets.json'}):
        path = app.data_dir / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b'<absent>')
    return digest.hexdigest()


def merge_ai(current, incoming):
    """An absent role belongs to the other client; keep its provider and key intact."""
    result = ai_config.validate(incoming)
    current = ai_config.validate(current)
    providers = {p['id']: p for p in result['providers']}
    old = {p['id']: p for p in current['providers']}
    for name, role in current['roles'].items():
        if name in result['roles']:
            continue
        preserved = dict(role)
        pid = role['provider_id']
        if pid != 'local':
            provider = old[pid]
            secret = current['secrets'].get(pid, '')
            if pid in providers and (providers[pid] != provider or result['secrets'].get(pid, '') != secret):
                suffix = hashlib.sha256(ai_config.encode({'provider': provider, 'secret': secret})).hexdigest()[:16]
                new_id = 'preserved-' + suffix
                while new_id in providers and (providers[new_id] != {**provider, 'id': new_id} or result['secrets'].get(new_id, '') != secret):
                    new_id = 'preserved-' + uuid.uuid4().hex
                pid = new_id
                preserved['provider_id'] = pid
            if pid not in providers:
                providers[pid] = {**provider, 'id': pid}
                result['providers'].append(providers[pid])
                if secret:
                    result['secrets'][pid] = secret
        result['roles'][name] = preserved
    return ai_config.validate(result)


def restore(app, value, job, expected_revision):
    """Validate before writes, retain protected recovery copies, roll back every target."""
    value = validate(value)
    with app.data_lock, app.settings.lock:
        if hasattr(app, 'data_sync'):
            app.data_sync.assert_idle()
        if revision(app) != expected_revision:
            raise ValueError('本机配置已变化，请重新预览备份')
        shared = merge_ai(ai_config.local_payload(app.settings), value['ai'])
        values = copy.deepcopy(value['config']) if value['platform'] == 'windows' else {}
        settings = values.get('settings.json', copy.deepcopy(app.settings.value))
        settings['providers'], settings['roles'] = shared['providers'], shared['roles']
        validate_config('settings.json', settings)
        values['settings.json'] = settings
        targets = set(values) | {'secrets.json'}
        recovery = app.data_dir.parent / (app.data_dir.name + '-recovery') / ('config-' + uuid.uuid4().hex)
        recovery.mkdir(parents=True)
        before = {name: (app.data_dir / name).read_bytes() if (app.data_dir / name).exists() else None for name in targets}
        # secrets.json contains only DPAPI ciphertext. Never persist decrypted API keys.
        for name, body in before.items():
            if body is not None:
                (recovery / name).write_bytes(body)
        try:
            job.check_cancelled()
            for name, config in values.items():
                atomic_json(app.data_dir / name, config)
            app.settings.reload()
            app.settings.import_secrets(shared['secrets'])
            if hasattr(app, 'reset_local_llm'):
                app.reset_local_llm()
            if hasattr(job, 'mark_committed'):
                job.mark_committed()
        except BaseException:
            for name, body in before.items():
                target = app.data_dir / name
                if body is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(body)
            app.settings.reload()
            raise
        return {'scope': 'config', 'files': len(values), 'include_secrets': True, 'recovery_path': str(recovery),
                'restart_recommended': True, 'message': '配置已恢复，请重启工具箱。' if value['platform'] == 'windows' else '共享 AI 配置已恢复，本机偏好保持不变。'}
