"""Manual immutable WebDAV snapshots; credentials never enter portable backups."""
import contextlib
import copy
import datetime
import email.utils
import json
import re
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx

from ..settings import atomic_json, protect
from ..webdav_layout import ensure_service_directory, service_config, service_paths, promote_legacy_connection, remember_ledger_source


DEFAULTS = {'url': '', 'remote_path': 'WinToolbox', 'username': '', 'include_media': True,
            'include_models': False, 'include_secrets': True}
SNAPSHOT = re.compile(r'^(?:wintoolbox-\d{8}T\d{6}Z-[a-f0-9]{32}(?:\.enc)?\.wtbak|config-(?:windows|android)-\d{8}-\d{6}-[a-f0-9]{32}\.wtconfig\.json)$')
PROPFIND = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>'
MAX_SNAPSHOT = 1024 ** 4


def normalized_url(value):
    try:
        parsed = urlsplit(str(value or '').strip())
        host = parsed.hostname or ''
        port = parsed.port
        if (parsed.scheme not in ('https', 'http') or not host
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError()
        parts = unquote(parsed.path).split('/')
        if any(part in ('.', '..') or '\\' in part or any(ord(c) < 32 for c in part) for part in parts):
            raise ValueError()
        if port == (443 if parsed.scheme == 'https' else 80):
            port = None
        netloc = ('[' + host.lower() + ']' if ':' in host else host.lower()) + (':' + str(port) if port else '')
        path = '/'.join(quote(part, safe='') for part in parts).rstrip('/') + '/'
        return urlunsplit((parsed.scheme, netloc, path, '', ''))
    except (ValueError, TypeError) as exc:
        raise ValueError('请填写 HTTP/HTTPS WebDAV 地址，不要在地址中包含账号、密码或查询参数') from exc


def normalized_path(value):
    value = str(value or '').strip().strip('/')
    parts = value.split('/')
    if (not value or len(value) > 500 or len(parts) > 20
            or any(not part or part in ('.', '..') or '\\' in part or '%' in part or any(ord(c) < 32 for c in part) for part in parts)):
        raise ValueError('远端目录无效，请填写普通文件夹路径，不要使用 .. 或 URL 编码')
    return '/'.join(parts)


def snapshot_name(value):
    if not isinstance(value, str) or not SNAPSHOT.fullmatch(value):
        raise ValueError('无效的远端快照名称')
    return value


class Remote:
    def __init__(self, config):
        config = {**config, 'url': normalized_url(config['url']), 'remote_path': normalized_path(config['remote_path'])}
        self.config = config
        encoded = '/'.join(quote(part, safe='') for part in config['remote_path'].split('/'))
        self.directory = config['url'] + encoded + '/'
        self.parent = self.directory.rsplit('/', 2)[0] + '/'
        self.client = httpx.Client(auth=httpx.BasicAuth(config['username'], protect(config['password_dpapi'], decrypt=True)),
                                  follow_redirects=False, timeout=httpx.Timeout(20, connect=10))

    def close(self):
        self.client.close()

    def checked(self, response, expected=(200, 201, 204, 207)):
        if response.status_code not in expected:
            status = response.status_code
            message = '服务器要求跳转，请直接填写最终 WebDAV 地址' if 300 <= status < 400 else '账号或密码无效' if status in (401, 403) else '目录不存在' if status == 404 else '服务器拒绝请求'
            raise ValueError(f'WebDAV {message}（HTTP {status}）')
        return response

    def request(self, method, url, **kwargs):
        try:
            return self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise RuntimeError(f'WebDAV 连接失败（{type(exc).__name__}），请检查地址、网络和证书') from None

    def xml(self, url, depth):
        try:
            with self.client.stream('PROPFIND', url, headers={'Depth': str(depth), 'Content-Type': 'application/xml; charset=utf-8'}, content=PROPFIND) as response:
                if response.status_code == 404:
                    return None
                self.checked(response, (207,))
                data = bytearray()
                for block in response.iter_bytes():
                    data.extend(block)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError('WebDAV 目录响应过大')
        except httpx.HTTPError as exc:
            raise RuntimeError(f'WebDAV 读取失败（{type(exc).__name__}）') from None
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError('WebDAV 目录响应包含不支持的 XML 声明')
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise ValueError('WebDAV 返回了无效目录信息') from exc

    def ensure_directory(self):
        if ensure_service_directory(self):
            return
        result = self.xml(self.directory, 0)
        if result is not None:
            if not result.findall('.//{DAV:}collection'):
                raise ValueError('远端目标不是文件夹')
            return
        parent = self.xml(self.parent, 0)
        if parent is None or not parent.findall('.//{DAV:}collection'):
            raise ValueError('远端父文件夹不存在，请先在网盘中创建；工具箱只创建最后一级目录')
        response = self.request('MKCOL', self.directory)
        # Another authorized client may have created the same application folder.
        self.checked(response, (201, 405))
        result = self.xml(self.directory, 0)
        if result is None or not result.findall('.//{DAV:}collection'):
            raise ValueError('WebDAV 工具箱文件夹未创建成功')

    def listing(self, pattern=SNAPSHOT):
        root = self.xml(self.directory, 1)
        if root is None:
            return []
        directory = urlsplit(self.directory)
        result = []
        for response in root.findall('{DAV:}response'):
            href = response.findtext('{DAV:}href', '')
            resolved = urlsplit(urljoin(self.directory, href))
            path = unquote(resolved.path)
            parent, _, name = path.rpartition('/')
            if (resolved.scheme != directory.scheme or resolved.netloc != directory.netloc
                    or parent + '/' != unquote(directory.path) or not pattern.fullmatch(name)):
                continue
            props = next((node.find('{DAV:}prop') for node in response.findall('{DAV:}propstat')
                          if ' 200 ' in node.findtext('{DAV:}status', '')), None)
            if props is None or props.find('.//{DAV:}collection') is not None:
                continue
            try:
                size = int(props.findtext('{DAV:}getcontentlength', '0'))
            except ValueError:
                continue
            if not 0 < size <= MAX_SNAPSHOT:
                continue
            modified = None
            with contextlib.suppress(ValueError, TypeError, OverflowError):
                modified = email.utils.parsedate_to_datetime(props.findtext('{DAV:}getlastmodified')).isoformat()
            result.append({'name': name, 'size': size, 'modified_at': modified, 'encrypted': name.endswith(('.enc.wtbak', '.wtconfig.json'))})
        return sorted(result, key=lambda row: row['name'], reverse=True)

    def upload(self, path, name, job):
        self.ensure_directory()
        temporary = self.directory + '.upload-' + uuid.uuid4().hex + '.part'
        destination = self.directory + quote(snapshot_name(name), safe='')
        size, sent = path.stat().st_size, 0
        if size > MAX_SNAPSHOT:
            raise ValueError('快照超过最大传输大小')
        def blocks():
            nonlocal sent
            with path.open('rb') as source:
                while block := source.read(1024 * 1024):
                    job.check_cancelled()
                    sent += len(block)
                    job.progress(45 + sent / max(1, size) * 50, '正在上传 WebDAV 快照')
                    yield block
        try:
            response = self.request('PUT', temporary, headers={'Content-Length': str(size), 'Content-Type': 'application/octet-stream', 'If-None-Match': '*'}, content=blocks())
            self.checked(response, (201, 204))
            job.check_cancelled()
            response = self.request('MOVE', temporary, headers={'Destination': destination, 'Overwrite': 'F'})
            if response.status_code == 502:
                job.check_cancelled()
                response = self.request('MOVE', temporary, headers={'Destination': urlsplit(destination).path, 'Overwrite': 'F'})
            self.checked(response, (201, 204))
            if hasattr(job, 'mark_committed'):
                job.mark_committed()
        finally:
            # Only this operation's random temporary object is ever removed.
            with contextlib.suppress(Exception):
                self.request('DELETE', temporary)
        return size

    def download(self, name, path, job):
        url = self.directory + quote(snapshot_name(name), safe='')
        received = 0
        try:
            with self.client.stream('GET', url, headers={'Accept-Encoding': 'identity'}) as response:
                self.checked(response, (200,))
                raw_length = response.headers.get('content-length')
                try:
                    length = int(raw_length) if raw_length else None
                except ValueError:
                    raise ValueError('远端快照大小无效') from None
                if length is not None and not 0 < length <= MAX_SNAPSHOT:
                    raise ValueError('远端快照大小无效')
                with path.open('xb') as output:
                    for block in response.iter_bytes(1024 * 1024):
                        job.check_cancelled()
                        received += len(block)
                        if received > MAX_SNAPSHOT:
                            raise ValueError('远端快照过大')
                        output.write(block)
                        job.progress(min(35, received / length * 35) if length else 20, '正在下载 WebDAV 快照')
                if not received or length is not None and received != length:
                    raise ValueError('WebDAV 快照下载不完整')
        except httpx.HTTPError as exc:
            raise RuntimeError(f'WebDAV 下载失败（{type(exc).__name__}）') from None


class StageJob:
    def __init__(self, job, offset, scale, artifacts=False):
        self.job, self.offset, self.scale, self.artifacts = job, offset, scale, artifacts

    def __getattr__(self, name):
        return getattr(self.job, name)

    def progress(self, percent, message):
        self.job.progress(self.offset + percent * self.scale, message)

    def artifact(self, *args, **kwargs):
        if self.artifacts:
            return self.job.artifact(*args, **kwargs)


def register(app):
    promote_legacy_connection(app.data_dir)
    path = app.data_dir / 'webdav.json'
    lock = threading.RLock()
    sync_lock = threading.Lock()
    credentials = {}
    previews = {}

    def config():
        saved = json.loads(path.read_text('utf-8')) if path.exists() else {}
        return {**DEFAULTS, **saved}

    def get(_=None):
        with lock:
            value = config()
            return {**{key: value[key] for key in DEFAULTS}, 'has_password': bool(value.get('password_dpapi')),
                    'service_paths': service_paths(value),
                    'configured': bool(value['url'] and value['username'] and value.get('password_dpapi'))}

    def save(params):
        with lock:
            previous = config()
            value = {**previous, **{key: params[key] for key in DEFAULTS if key in params}}
            value['url'] = normalized_url(value['url'])
            value['remote_path'] = normalized_path(value['remote_path'])
            value['username'] = str(value['username']).strip()
            if not value['username'] or len(value['username']) > 256 or any(ord(c) < 32 for c in value['username']):
                raise ValueError('请填写有效的 WebDAV 用户名')
            for flag in ('include_media', 'include_models', 'include_secrets'):
                if not isinstance(value[flag], bool):
                    raise ValueError('同步范围选项无效')
            password = params.get('password', '')
            if not isinstance(password, str):
                raise ValueError('WebDAV 密码应为文本')
            if params.get('clear_password') or value['url'] != previous['url'] or value['username'] != previous['username']:
                value.pop('password_dpapi', None)
            if password:
                value['password_dpapi'] = protect(password)
            if previous.get('url') and previous.get('username') and previous.get('password_dpapi') and any(value.get(key) != previous.get(key) for key in ('url', 'username', 'remote_path')):
                if hasattr(app, 'relay'):
                    app.relay.remember_source(service_config(previous, 'relay'))
                remember_ledger_source(app.data_dir, previous, value)
            atomic_json(path, value)
            if hasattr(app, 'data_sync'):
                if any(value.get(key) != previous.get(key) for key in ('url', 'username', 'remote_path', 'password_dpapi')):
                    app.data_sync.connection_changed()
                else:
                    app.data_sync.request()
            return get()

    def require_config():
        with lock:
            value = config()
        value['url'] = normalized_url(value['url'])
        value['remote_path'] = normalized_path(value['remote_path'])
        if not value['username'] or not value.get('password_dpapi'):
            raise ValueError('请先保存 WebDAV 用户名和密码')
        return value

    @contextlib.contextmanager
    def remote(value=None):
        client = Remote(value or require_config())
        try:
            yield client
        finally:
            client.close()

    def test(_):
        with remote() as client:
            client.ensure_directory()
            return {'ok': True, 'message': 'WebDAV 连接成功', 'remote_path': client.config['remote_path']}

    def listing(_):
        value = require_config()
        with remote(service_config(value, 'configuration')) as client:
            client.ensure_directory()
            snapshots = [{**row, 'scope': 'config', 'location': 'unified', 'kind': 'unified',
                          'source_platform': 'android' if row['name'].startswith('config-android-') else 'windows'}
                         for row in client.listing() if row['name'].endswith('.wtconfig.json')]
        with remote(service_config(value, 'config_backups')) as client:
            snapshots.extend({**row, 'scope': 'config', 'location': 'config'} for row in client.listing())
        with remote(value) as client:
            snapshots.extend({**row, 'scope': 'legacy_full', 'location': 'legacy_root'} for row in client.listing())
        with remote(service_config(value, 'ai_config')) as client:
            snapshots.extend({**row, 'encrypted': True, 'scope': 'config', 'location': 'legacy_ai', 'kind': 'legacy_ai',
                              'source_platform': 'shared'} for row in client.listing(re.compile(r'^config-v1\.json$')))
        return {'snapshots': sorted(snapshots, key=lambda row: row.get('modified_at') or row['name'], reverse=True),
                'remote_path': service_config(value, 'configuration')['remote_path']}

    def identity(value):
        import hashlib
        return hashlib.sha256(json.dumps({k: value.get(k) for k in ('url', 'username', 'remote_path', 'password_dpapi')}, sort_keys=True).encode()).hexdigest()

    def preview(params):
        import time
        from .. import config_snapshots, ai_config
        value = require_config()
        location, name = params.get('location'), params.get('name')
        if location == 'unified':
            snapshot_name(name)
            if not name.endswith('.wtconfig.json'):
                raise ValueError('配置备份名称无效')
            target = service_config(value, 'configuration')
        elif location == 'legacy_ai' and name == 'config-v1.json':
            target = service_config(value, 'ai_config')
        else:
            raise ValueError('请选择可预览的配置备份')
        with app.data_lock, app.settings.lock:
            before = config_snapshots.revision(app)
        with remote(target) as client:
            body = bytearray()
            with client.client.stream('GET', client.directory + quote(name, safe='')) as response:
                client.checked(response, (200,))
                for block in response.iter_bytes(65536):
                    body.extend(block)
                    if len(body) > config_snapshots.LIMIT:
                        raise ValueError('配置备份过大')
        password = protect(value['password_dpapi'], decrypt=True)
        if location == 'legacy_ai':
            shared = ai_config.decrypt(ai_config.parse(body), password)
            payload = {'format': config_snapshots.FORMAT, 'version': 2, 'platform': 'android',
                       'created_at': 0, 'config': {}, 'ai': shared}
        else:
            payload = config_snapshots.decrypt(config_snapshots.parse(body), password)
        token = uuid.uuid4().hex
        with lock:
            for key in list(previews):
                if time.monotonic() - previews[key]['at'] > 900:
                    previews.pop(key)
            if len(previews) >= 8:
                previews.pop(next(iter(previews)))
            previews[token] = {'payload': payload, 'revision': before, 'identity': identity(value),
                               'name': name, 'location': location, 'at': time.monotonic()}
        return {**ai_config.preview(payload['ai']), 'token': token, 'source_platform': payload['platform'],
                'scope': 'config', 'created_at': payload['created_at'], 'same_platform': payload['platform'] == 'windows',
                'message': '恢复本机设置及共享 AI 配置，应用数据不变。' if payload['platform'] == 'windows' else '仅恢复共享 AI 配置，本机偏好和应用数据不变。'}

    def secret_for(job):
        with lock:
            value = credentials.get(job.params.get('credential_token'))
            if not value:
                raise ValueError('同步密码不保存在任务记录中，请重新发起同步')
            return copy.deepcopy(value)

    def upload_job(job):
        from .. import config_snapshots
        saved = secret_for(job)
        if not sync_lock.acquire(blocking=False):
            raise ValueError('已有配置备份任务正在运行')
        try:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
            name = 'config-windows-' + stamp + '-' + uuid.uuid4().hex + '.wtconfig.json'
            with tempfile.TemporaryDirectory(prefix='.webdav-upload-', dir=app.data_dir.parent) as directory:
                source = Path(directory) / name
                with app.data_lock, app.settings.lock:
                    payload = config_snapshots.snapshot(app)
                source.write_bytes(config_snapshots.encode(config_snapshots.encrypt(payload, saved['password'])))
                with remote(saved['config']) as client:
                    size = client.upload(source, name, job)
            return {'name': name, 'size': size, 'encrypted': True, 'remote_path': saved['config']['remote_path'],
                    'scope': 'config', 'files': len(payload['config']), 'warnings': []}
        finally:
            sync_lock.release()
            with lock:
                credentials.pop(job.params.get('credential_token'), None)

    def restore_job(job):
        from ..config_backup import import_config
        saved = secret_for(job)
        name = job.params.get('name')
        if not sync_lock.acquire(blocking=False):
            raise ValueError('已有 WebDAV 同步任务正在运行')
        try:
            if 'preview' in saved:
                from .. import config_snapshots
                item = saved['preview']
                if identity(require_config()) != item['identity']:
                    raise ValueError('WebDAV 连接已变化，请重新预览')
                return config_snapshots.restore(app, item['payload'], job, item['revision'])
            snapshot_name(name)
            with tempfile.TemporaryDirectory(prefix='.webdav-download-', dir=app.data_dir.parent) as directory:
                source = Path(directory) / name
                with remote(saved['config']) as client:
                    client.download(name, source, job)
                with getattr(app, 'data_lock', contextlib.nullcontext()), app.settings.lock:
                    result = import_config(app, source, StageJob(job, 35, .6), password=saved['password'])
            job.progress(100, 'WebDAV 配置已恢复，记账和文件数据保持完整')
            return {**result, 'remote_name': name}
        finally:
            sync_lock.release()
            with lock:
                credentials.pop(job.params.get('credential_token'), None)

    def submit(params, restore=False):
        import time
        value = require_config()
        location = params.get('location', 'config') if restore else 'unified'
        previewed = None
        if restore and location in ('unified', 'legacy_ai'):
            with lock:
                previewed = previews.pop(params.get('preview_token'), None)
            if (not previewed or time.monotonic() - previewed['at'] > 900 or previewed['name'] != params.get('name')
                    or previewed['location'] != location or previewed['identity'] != identity(value)):
                raise ValueError('预览已失效，请重新预览配置备份')
            name = previewed['name']
            password = ''
        elif restore:
            name = snapshot_name(params.get('name'))
            if location not in ('config', 'legacy_root'):
                raise ValueError('配置备份位置无效')
            password = params.get('backup_password', '')
            if not isinstance(password, str):
                raise ValueError('备份密码应为文本')
        else:
            name = None
            password = protect(value['password_dpapi'], decrypt=True)
        if location == 'unified':
            value = service_config(value, 'configuration')
        elif location == 'config':
            value = service_config(value, 'config_backups')
        token = uuid.uuid4().hex
        with lock:
            credentials[token] = {'config': value, 'password': password, **({'preview': previewed} if previewed else {})}
        safe = {'credential_token': token, **({'name': name, 'location': location} if restore else {})}
        try:
            return app.jobs.submit('webdav.restore' if restore else 'webdav.upload', safe)
        except BaseException:
            with lock:
                credentials.pop(token, None)
            raise

    app.jobs.register('webdav.upload', upload_job)
    app.jobs.register('webdav.restore', restore_job)
    for name, handler in (('get', get), ('save', save), ('test', test), ('list', listing), ('preview', preview)):
        app.register('webdav.' + name, handler)
    app.register('webdav.upload', lambda p: submit(p))
    app.register('webdav.restore', lambda p: submit(p, restore=True))
