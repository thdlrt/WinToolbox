"""A scoped WebDAV file inbox. Network work is always a cancellable job."""
import contextlib
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
import uuid
import xml.etree.ElementTree as ET

import httpx

from ..settings import atomic_json, protect
from .filesync import safe_path, signature
from .webdav import Remote as SnapshotRemote

PROPERTIES = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:getetag/></d:prop></d:propfind>'
MAX_FILES = 20000
REGISTRY_ROOTS = (r'Software\Classes\*\shell\WinToolboxRelay', r'Software\Classes\Directory\shell\WinToolboxRelay')


def relative(value, empty=True):
    if not isinstance(value, str):
        raise ValueError('目录应为文本')
    if not value and empty:
        return ''
    parts = value.split('/')
    if (not value or len(value) > 2000 or any(not p or p in ('.', '..') or
            any(c in p for c in '\\:*?"<>|') or any(ord(c) < 32 for c in p) or p.endswith((' ', '.')) or
            re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I) for p in parts)):
        raise ValueError('文件路径无效，不支持越界路径或 Windows 保留名称')
    return value


def endpoint(value):
    try:
        p = urlsplit(str(value).strip())
        if p.scheme not in ('http', 'https') or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment:
            raise ValueError()
        _ = p.port
        decoded = unquote(p.path).strip('/')
        relative(decoded)
        return urlunsplit((p.scheme, p.netloc, '/' + '/'.join(quote(x, safe='') for x in decoded.split('/') if x) + ('/' if decoded else ''), '', ''))
    except (ValueError, TypeError) as exc:
        raise ValueError('请填写 HTTP/HTTPS WebDAV 地址，账号密码填在独立输入框中') from exc


def strong_etag(value):
    return isinstance(value, str) and value.startswith('"') and value.endswith('"') and not any(ord(c) < 32 for c in value)


class Remote(SnapshotRemote):
    def __init__(self, config):
        self.config = config
        base = endpoint(config['url'])
        folder = relative(config['remote_path'], empty=False)
        self.directory = base + '/'.join(quote(x, safe='') for x in folder.split('/')) + '/'
        self.client = httpx.Client(auth=httpx.BasicAuth(config['username'], protect(config['password_dpapi'], decrypt=True)),
                                  follow_redirects=False, timeout=httpx.Timeout(60, connect=10), trust_env=False)

    def url(self, path='', directory=False):
        path = relative(path)
        return self.directory + '/'.join(quote(x, safe='') for x in path.split('/')) + ('/' if directory and path else '')

    def rows(self, path='', depth=1, job=None):
        url = self.url(path, directory=depth == 1)
        try:
            with self.propfind(url, depth) as response:
                if response.status_code == 404:
                    return None
                self.checked(response, (207,))
                data = bytearray()
                for block in response.iter_bytes():
                    if job: job.check_cancelled()
                    data.extend(block)
                    if len(data) > 8 * 1024 * 1024:
                        raise ValueError('目录过大，请按子目录查看')
        except httpx.HTTPError as exc:
            raise RuntimeError(f'WebDAV 读取失败（{type(exc).__name__}）') from None
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError('不支持的 WebDAV XML 声明')
        try:
            document = ET.fromstring(data)
        except ET.ParseError:
            raise ValueError('服务器返回的不是有效 WebDAV 目录') from None
        base = urlsplit(self.directory)
        base_path = unquote(base.path)
        result = {}
        for response in document.findall('{DAV:}response'):
            href = urlsplit(urljoin(url, response.findtext('{DAV:}href', '')))
            decoded = unquote(href.path)
            if href.scheme != base.scheme or href.netloc != base.netloc or href.query or href.fragment or not decoded.startswith(base_path):
                continue
            name = decoded[len(base_path):].rstrip('/')
            relative(name)
            if name != path and (depth == 0 or name.rpartition('/')[0] != path):
                continue
            props = next((x.find('{DAV:}prop') for x in response.findall('{DAV:}propstat') if ' 200 ' in x.findtext('{DAV:}status', '')), None)
            if props is None:
                raise ValueError('部分文件属性不可读，未把不完整目录当作完整结果')
            is_dir = props.find('.//{DAV:}collection') is not None
            modified = None
            with contextlib.suppress(ValueError, TypeError, OverflowError):
                modified = email.utils.parsedate_to_datetime(props.findtext('{DAV:}getlastmodified')).timestamp()
            try:
                size = int(props.findtext('{DAV:}getcontentlength', '0'))
                if size < 0: raise ValueError()
            except ValueError:
                raise ValueError('服务器返回无效的文件大小') from None
            result[name] = {'path': name, 'name': name.rsplit('/', 1)[-1], 'directory': is_dir,
                            'size': size, 'modified': modified, 'etag': props.findtext('{DAV:}getetag', '')}
        if path not in result:
            raise ValueError('服务器没有返回请求目录的属性，无法确认结果完整')
        return result

    @contextlib.contextmanager
    def propfind(self, url, depth):
        # Some NAS servers canonicalize collection URLs with one trailing slash.
        # Follow only that exact same-origin spelling, never arbitrary redirects with credentials.
        kwargs = {'headers': {'Depth': str(depth), 'Content-Type': 'application/xml'}, 'content': PROPERTIES}
        with self.client.stream('PROPFIND', url, **kwargs) as response:
            redirect = response.status_code in (301, 302, 307, 308) and not url.endswith('/') and urljoin(url, response.headers.get('location', '')) == url + '/'
            if not redirect:
                yield response
                return
        with self.client.stream('PROPFIND', url + '/', **kwargs) as response:
            yield response

    def stat(self, path, job=None):
        rows = self.rows(path, 0, job)
        return rows.get(path) if rows else None

    def listing(self, path='', job=None):
        rows = self.rows(path, 1, job)
        if rows is None: raise ValueError('远端文件夹不存在，请检查中转目录设置')
        if not rows[path]['directory']: raise ValueError('所选路径不是文件夹')
        return sorted((row for key, row in rows.items() if key != path), key=lambda r: (not r['directory'], -(r['modified'] or 0), r['name'].casefold()))

    def ensure(self, path='', job=None):
        existing = self.stat(path, job)
        if existing:
            if not existing['directory']: raise ValueError('目标位置已有同名文件')
            return
        # Only the configured final root or a descendant can be created.
        if path and '/' in path: self.ensure(path.rpartition('/')[0], job)
        if job: job.check_cancelled()
        response = self.request('MKCOL', self.url(path, True))
        if response.status_code == 409: raise ValueError('父目录不存在，请先在飞牛上建立中转目录的父目录')
        self.checked(response, (201, 405))
        if not (self.stat(path, job) or {}).get('directory'):
            configured = self.config['remote_path'] + ('/' + path if path else '')
            raise ValueError(f'无法访问或创建远端目录「{configured}」。请检查完整路径；若飞牛根目录为共享文件夹列表，需要填写「共享名/子目录」')

    def walk(self, path, job, directories=None):
        queue, files, visited = [path], [], set()
        while queue:
            job.check_cancelled()
            current = queue.pop()
            if current in visited: raise ValueError('目录循环，已停止遍历')
            visited.add(current)
            if directories is not None: directories.add(current)
            for row in self.listing(current, job):
                if row['directory']: queue.append(row['path'])
                else: files.append(row)
            if len(files) + len(visited) + len(queue) > MAX_FILES:
                raise ValueError('单次最多处理 20000 个文件或目录，请缩小范围')
        return files

    def upload_file(self, source, destination, job):
        expected = signature(source, job.check_cancelled)
        if expected is None: raise ValueError('本地文件已不存在')
        parent = destination.rpartition('/')[0]
        self.ensure(parent, job)
        if self.stat(destination, job): raise ValueError(f'远端已有同名项目，未覆盖：{destination}')
        temporary = (parent + '/' if parent else '') + '.relay-upload-' + uuid.uuid4().hex + '.part'
        sent = 0
        digest = hashlib.sha256()
        def blocks():
            nonlocal sent
            with source.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    job.check_cancelled()
                    digest.update(block)
                    sent += len(block)
                    job.progress(min(90, sent / max(1, expected['size']) * 90), f'上传 {source.name}（{sent}/{expected["size"]} 字节）')
                    yield block
        try:
            self.checked(self.request('PUT', self.url(temporary), headers={'Content-Length': str(expected['size']), 'If-None-Match': '*', 'Content-Type': 'application/octet-stream'}, content=blocks()), (201, 204))
            if sent != expected['size'] or digest.hexdigest() != expected['hash'] or signature(source, job.check_cancelled) != expected:
                raise ValueError('上传时本地文件发生变化，未发布文件')
            job.check_cancelled()
            moved = self.request('MOVE', self.url(temporary), headers={'Destination': self.url(destination), 'Overwrite': 'F'})
            if moved.status_code == 502:
                # fnOS external reverse proxies can reject their own public authority
                # in Destination. Keep the move on this origin and never overwrite.
                job.check_cancelled()
                moved = self.request('MOVE', self.url(temporary), headers={'Destination': urlsplit(self.url(destination)).path, 'Overwrite': 'F'})
            self.checked(moved, (201, 204))
        finally:
            with contextlib.suppress(Exception): self.request('DELETE', self.url(temporary))
        remote = self.stat(destination, job)
        if not remote or remote['size'] != expected['size']:
            raise ValueError('远端文件大小未通过校验，本地文件已保留')
        return expected, remote

    def download_file(self, row, output, job):
        headers = {'Accept-Encoding': 'identity'}
        if strong_etag(row['etag']): headers['If-Match'] = row['etag']
        received, digest = 0, hashlib.sha256()
        try:
            with self.client.stream('GET', self.url(row['path']), headers=headers) as response:
                self.checked(response, (200,))
                with output.open('xb') as stream:
                    for block in response.iter_bytes(1024 * 1024):
                        job.check_cancelled()
                        received += len(block)
                        if received > row['size']: raise ValueError('远端文件在下载时变大，请刷新后重试')
                        digest.update(block)
                        stream.write(block)
                        job.progress(min(95, received / max(1, row['size']) * 95), f'下载 {row["name"]}')
                    stream.flush()
                    os.fsync(stream.fileno())
            if received != row['size'] or self.stat(row['path'], job) != row:
                raise ValueError('远端文件发生变化或下载不完整，请重试')
            return digest.hexdigest()
        except httpx.HTTPError as exc:
            raise RuntimeError(f'WebDAV 下载失败（{type(exc).__name__}）') from None


def notify_shell_associations():
    """Invalidate Explorer's cached verbs after a per-user registration change."""
    if os.name == 'nt':
        import ctypes
        notify = ctypes.windll.shell32.SHChangeNotify
        notify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        notify.restype = None
        notify(0x08000000, 0x1000, None, None)  # SHCNE_ASSOCCHANGED | SHCNF_FLUSH


def sendto_path():
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 9, None, 0, buffer):
        raise ValueError('无法定位 Windows 发送到目录')
    return Path(buffer.value) / '文件中转站.lnk'


def sendto_shortcut(enabled=None, exe=None):
    path = sendto_path()
    if enabled is True:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Paths travel as environment values, never interpolated into executable script text.
        script = "$ErrorActionPreference='Stop'; $s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut($env:WINTOOLBOX_RELAY_LINK); $l.TargetPath=$env:WINTOOLBOX_RELAY_EXE; $l.Arguments='--relay-upload'; $l.WorkingDirectory=[IO.Path]::GetDirectoryName($env:WINTOOLBOX_RELAY_EXE); $l.Save()"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            env={**os.environ, 'WINTOOLBOX_RELAY_LINK': str(path), 'WINTOOLBOX_RELAY_EXE': str(exe)},
            capture_output=True, timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not path.is_file(): raise ValueError('Windows 发送到快捷方式创建失败')
    elif enabled is False:
        path.unlink(missing_ok=True)
    return path.is_file()


def context_menu(enabled=None):
    if os.name != 'nt':
        return {'enabled': False, 'supported': False}
    import winreg
    exe = Path(os.environ.get('WINTOOLBOX_APP_EXE', ''))
    valid = exe.is_absolute() and exe.is_file() and exe.suffix.lower() == '.exe'
    command = f'"{exe}" --relay-upload "%1"'
    if enabled is not None:
        if not isinstance(enabled, bool) or not valid: raise ValueError('请在已打包的 Windows 工具箱中设置右键菜单')
        for root in REGISTRY_ROOTS:
            if enabled:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
                    winreg.SetValueEx(key, '', 0, winreg.REG_SZ, '发送到文件中转站')
                    winreg.SetValueEx(key, 'MUIVerb', 0, winreg.REG_SZ, '发送到文件中转站')
                    winreg.SetValueEx(key, 'Position', 0, winreg.REG_SZ, 'Top')
                    winreg.SetValueEx(key, 'Icon', 0, winreg.REG_SZ, str(exe))
                    winreg.SetValueEx(key, 'MultiSelectModel', 0, winreg.REG_SZ, 'Document')
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r'\command') as key:
                    winreg.SetValueEx(key, '', 0, winreg.REG_SZ, command)
            else:
                for key in (root + r'\command', root):
                    with contextlib.suppress(FileNotFoundError): winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        sendto_shortcut(enabled, exe)
        notify_shell_associations()
    values = []
    for root in REGISTRY_ROOTS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root + r'\command') as key:
                values.append(winreg.QueryValueEx(key, '')[0] == command)
        except FileNotFoundError: values.append(False)
    return {'enabled': all(values), 'supported': valid, 'send_to_enabled': sendto_shortcut()}


class Relay:
    def __init__(self, app):
        self.app = app
        self.path = app.data_dir / 'relay-connection.json'
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.plans = {}
        self.launch = None

    def menu(self, params):
        with self.lock:
            result = context_menu(params.get('enabled'))
            if isinstance(params.get('enabled'), bool):
                atomic_json(self.app.data_dir / 'relay-menu.json', {'enabled': params['enabled'], 'exe': os.environ.get('WINTOOLBOX_APP_EXE'), 'version': 1})
            return result

    def initialize_menu(self, params):
        with self.lock:
            path = self.app.data_dir / 'relay-menu.json'
            saved = json.loads(path.read_text('utf-8')) if path.exists() else {}
            if saved.get('enabled') is False:
                return {'enabled': False}
            state = context_menu()
            if not state['supported']:
                return state
            exe = os.environ.get('WINTOOLBOX_APP_EXE')
            if not state['enabled'] or not state.get('send_to_enabled') or saved.get('exe') != exe or saved.get('version') != 1:
                return self.menu({'enabled': True})
            return state

    def config(self):
        with self.lock:
            data = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {}
        return {'url': '', 'username': '', 'remote_path': '文件中转站',
                'download_dir': str(Path.home() / 'Downloads' / '文件中转站'), **data}

    def get(self, _=None):
        value = self.config()
        return {k: v for k, v in value.items() if k != 'password_dpapi'} | {
            'has_password': bool(value.get('password_dpapi')), 'configured': bool(value['url'] and value['username'] and value.get('password_dpapi')),
            'context_menu': context_menu()}

    def save(self, params):
        if not self.write_lock.acquire(blocking=False):
            raise ValueError("上传或清理正在进行，请等待完成后修改连接")
        try:
            with self.lock:
                old = self.config()
                value = {**old, **{k: params[k] for k in ('url', 'username', 'remote_path', 'download_dir') if k in params}}
                value['url'] = endpoint(value['url'])
                value['remote_path'] = relative(value['remote_path'].strip('/'), empty=False)
                value['download_dir'] = str(safe_path(value['download_dir']))
                if not isinstance(value['username'], str) or not value['username'].strip() or any(ord(c) < 32 for c in value['username']):
                    raise ValueError('请填写有效用户名')
                password = params.get('password', '')
                if not isinstance(password, str): raise ValueError('密码应为文本')
                if value['url'] != old['url'] or value['username'] != old['username'] or params.get('clear_password'):
                    value.pop('password_dpapi', None)
                if password: value['password_dpapi'] = protect(password)
                value['revision'] = uuid.uuid4().hex
                atomic_json(self.path, value)
                self.plans.clear()
            return self.get()
        finally:
            self.write_lock.release()

    def submit(self, method, params):
        # Allowlisted arguments keep account credentials out of job records and retries.
        fields = {'list': ('path',), 'test': (), 'mkdir': ('path',), 'upload': ('paths', 'path', 'move'),
                  'download': ('paths',), 'prepare_drag': ('paths',), 'delete': ('entries',), 'cleanup_preview': ('days',), 'cleanup': ('token',)}
        safe = {key: params[key] for key in fields[method] if key in params}
        safe['revision'] = self.config().get('revision')
        return self.app.jobs.submit('relay.' + method, safe)

    @contextlib.contextmanager
    def remote(self, job):
        value = self.config()
        if not value['url'] or not value.get('password_dpapi'): raise ValueError('请先在文件中转站保存 WebDAV 地址、用户名和密码')
        if value.get('revision') != job.params.get('revision'): raise ValueError('连接配置已变化，请重新操作')
        remote = Remote(value)
        try: yield remote
        finally: remote.close()

    def run(self, method, job):
        guard = method in ('upload', 'cleanup', 'mkdir', 'delete')
        if guard:
            while not self.write_lock.acquire(timeout=.2): job.check_cancelled()
        try:
            with self.remote(job) as remote:
                if method == 'test':
                    remote.ensure('', job)
                    return {'message': '连接成功，中转目录可访问', 'entries': remote.listing('', job)}
                if method == 'list':
                    path = relative(job.params.get('path', ''))
                    return {'path': path, 'entries': remote.listing(path, job)}
                if method == 'mkdir':
                    path = relative(job.params.get('path'), empty=False)
                    remote.ensure(path, job)
                    return {'path': path}
                if method == 'upload': return self.upload(remote, job)
                if method == 'download': return self.download(remote, job)
                if method == 'delete': return self.delete(remote, job)
                if method == 'prepare_drag':
                    root = safe_path(self.app.data_dir / 'relay-drag-cache' / uuid.uuid4().hex)
                    remote.config = {**remote.config, 'download_dir': str(root)}
                    self.download(remote, job)
                    return {'paths': [str(safe_path(root / relative(p, False))) for p in job.params['paths']]}
                if method == 'cleanup_preview': return self.cleanup_preview(remote, job)
                if method == 'cleanup': return self.cleanup(remote, job)
                raise ValueError('未知中转操作')
        finally:
            if guard: self.write_lock.release()

    def upload(self, remote, job):
        paths = job.params.get('paths')
        if not isinstance(paths, list) or not paths or len(paths) > 1000: raise ValueError('请选择 1–1000 个文件或文件夹')
        folder = relative(job.params.get('path', ''))
        move = job.params.get('move', False)
        if not isinstance(move, bool): raise ValueError('移动选项无效')
        remote.ensure(folder, job)
        files, directories, seen = [], set(), set()
        for raw in paths:
            source = safe_path(raw)
            relative(source.name, empty=False)
            prefix = (folder + '/' if folder else '') + source.name
            if source.is_file(): files.append((source, prefix))
            elif source.is_dir():
                directories.add(prefix)
                def fail(error): raise error
                for root, dirs, names in os.walk(source, followlinks=False, onerror=fail):
                    job.check_cancelled()
                    for name in dirs:
                        child = safe_path(Path(root) / name)
                        directories.add(relative(prefix + '/' + child.relative_to(source).as_posix(), False))
                    for name in names:
                        child = safe_path(Path(root) / name)
                        files.append((child, relative(prefix + '/' + child.relative_to(source).as_posix(), False)))
                    if len(files) + len(directories) > MAX_FILES: raise ValueError('单次最多上传 20000 项，请缩小范围')
            else: raise ValueError(f'本地路径不存在：{source}')
        for source, destination in files:
            relative(destination, False)
            if destination.casefold() in seen: raise ValueError('所选文件在远端重名，请分批上传')
            seen.add(destination.casefold())
        uploaded, moved, errors = [], [], []
        for path in sorted(directories, key=lambda p: (p.count('/'), p)):
            remote.ensure(path, job)
        for source, destination in files:
            job.check_cancelled()
            try:
                expected, row = remote.upload_file(source, destination, job)
                uploaded.append(destination)
                if move:
                    # GET verification, not an ETag-as-MD5 assumption, precedes local removal.
                    with tempfile.TemporaryDirectory(prefix='relay-verify-') as tmp:
                        actual = remote.download_file(row, Path(tmp) / 'check', job)
                    if actual != expected['hash'] or signature(source, job.check_cancelled) != expected:
                        raise ValueError('内容校验不一致，本地文件已保留')
                    job.check_cancelled()
                    safe_path(source).unlink()
                    moved.append(str(source))
            except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
                errors.append({'path': str(source), 'message': str(exc) if not isinstance(exc, httpx.HTTPError) else '网络传输失败，本地文件保留'})
        return {'uploaded': uploaded, 'moved': moved, 'errors': errors}

    def download(self, remote, job):
        paths = job.params.get('paths')
        if not isinstance(paths, list) or not paths or len(paths) > 1000: raise ValueError('请选择需要下载的项目')
        root = safe_path(remote.config['download_dir'])
        root.mkdir(parents=True, exist_ok=True)
        files, names, directories = [], set(), set()
        for path in paths:
            path = relative(path, False)
            row = remote.stat(path, job)
            if row is None: raise ValueError('远端项目已不存在，请刷新')
            entries = remote.walk(path, job, directories) if row['directory'] else [row]
            for entry in entries:
                if entry['path'] not in names:
                    files.append(entry); names.add(entry['path'])
            if len(files) + len(directories) > MAX_FILES: raise ValueError('单次最多下载 20000 项，请缩小范围')
        for directory in directories:
            target = safe_path(root / directory)
            if not target.is_relative_to(root): raise ValueError('下载目录越界')
            target.mkdir(parents=True, exist_ok=True)
        outputs = []
        for row in files:
            job.check_cancelled()
            destination = safe_path(root / row['path'])
            if not destination.is_relative_to(root): raise ValueError('下载路径越界')
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination = destination.with_name(destination.stem + '-' + uuid.uuid4().hex[:8] + destination.suffix)
            with tempfile.TemporaryDirectory(prefix='.relay-download-', dir=destination.parent) as temporary:
                staged = Path(temporary) / 'content'
                remote.download_file(row, staged, job)
                job.check_cancelled()
                safe_path(destination)
                # Atomic exclusive publication: a file created concurrently is never replaced.
                os.link(staged, destination)
            outputs.append(str(destination))
        return {'paths': outputs, 'directory': str(root)}

    def delete(self, remote, job):
        entries = job.params.get('entries')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
            raise ValueError('请选择 1–1000 个文件')
        for expected in entries:
            if not isinstance(expected, dict): raise ValueError('文件信息无效，请刷新')
            relative(expected.get('path'), False)
            if expected.get('directory'): raise ValueError('请进入文件夹选择文件删除；不会递归删除目录')
            if not strong_etag(expected.get('etag')): raise ValueError('服务器缺少可靠文件版本，无法确认删除；请在飞牛文件管理中处理')
        removed, skipped = [], []
        for expected in entries:
            job.check_cancelled()
            path = relative(expected.get('path'), False)
            if expected.get('directory'):
                raise ValueError('请进入文件夹选择文件删除；不会递归删除目录')
            if not strong_etag(expected.get('etag')):
                raise ValueError('服务器缺少可靠文件版本，无法确认删除；请在飞牛文件管理中处理')
            current = remote.stat(path, job)
            if not current or current['directory'] or any(current.get(k) != expected.get(k) for k in ('etag', 'size', 'modified')):
                skipped.append(path); continue
            response = remote.request('DELETE', remote.url(path), headers={'If-Match': expected['etag']})
            if response.status_code in (404, 412): skipped.append(path); continue
            remote.checked(response, (200, 204)); removed.append(path)
            job.progress(100 * (len(removed) + len(skipped)) / len(entries), f'已删除 {len(removed)} 个文件')
        return {'removed': removed, 'skipped': skipped}

    def cleanup_preview(self, remote, job):
        days = int(job.params.get('days', 7))
        if not 1 <= days <= 3650: raise ValueError('保留天数应为 1–3650')
        cutoff = time.time() - days * 86400
        files = remote.walk('', job)
        candidates = [row for row in files if row['modified'] is not None and row['modified'] < cutoff and strong_etag(row['etag'])]
        skipped = [row for row in files if row['modified'] is None or not strong_etag(row['etag'])]
        token = uuid.uuid4().hex
        with self.lock:
            self.plans = {key: v for key, v in self.plans.items() if v['expires'] > time.time()}
            if len(self.plans) >= 20: self.plans.pop(next(iter(self.plans)))
            self.plans[token] = {'revision': remote.config['revision'], 'expires': time.time() + 600, 'files': candidates}
        return {'token': token, 'files': candidates, 'bytes': sum(r['size'] for r in candidates), 'skipped': len(skipped), 'days': days}

    def cleanup(self, remote, job):
        with self.lock:
            plan = self.plans.pop(job.params.get('token'), None)
        if not plan or plan['expires'] < time.time() or plan['revision'] != remote.config['revision']:
            raise ValueError('清理预览已失效，请重新预览')
        removed, skipped = [], []
        for row in plan['files']:
            job.check_cancelled()
            current = remote.stat(row['path'], job)
            if current != row or current['directory']:
                skipped.append(row['path']); continue
            response = remote.request('DELETE', remote.url(row['path']), headers={'If-Match': row['etag']})
            if response.status_code in (404, 412): skipped.append(row['path']); continue
            remote.checked(response, (200, 204))
            removed.append(row['path'])
            job.progress(len(removed) / max(1, len(plan['files'])) * 100, f'已清理 {len(removed)} 个文件')
        return {'removed': removed, 'skipped': skipped}

    def enqueue(self, params):
        # Shell sends paths only. Destination always remains the configured inbox root.
        result = self.submit('upload', {'paths': params.get('paths'), 'path': '', 'move': False})
        self.launch = {'job_id': result['id']}
        self.app.emit('relay.open', job_id=result['id'])
        return result

    def pending(self, _):
        value, self.launch = self.launch, None
        return value


def register(app):
    service = Relay(app)
    app.relay = service
    app.register('relay.get', service.get)
    app.register('relay.save', service.save)
    app.register('relay.context_menu', service.menu)
    app.register('relay.initialize_menu', service.initialize_menu)
    app.register('relay.enqueue', service.enqueue)
    app.register('relay.pending', service.pending)
    for method in ('test', 'list', 'mkdir', 'upload', 'download', 'prepare_drag', 'delete', 'cleanup_preview', 'cleanup'):
        app.jobs.register('relay.' + method, lambda job, method=method: service.run(method, job))
        app.register('relay.' + method, lambda p, method=method: service.submit(method, p))
