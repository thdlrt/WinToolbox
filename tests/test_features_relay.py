import contextlib
import email.utils
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

import httpx
import pytest

from toolbox.app import App
from toolbox.features import relay
from toolbox.jobs import Cancelled


class Dav:
    def __init__(self):
        self.dirs = {'/dav/', '/dav/WinToolbox/', '/dav/WinToolbox/file-relay/'}
        self.files = {}
        self.modified = {}
        self.requests = []
        self.after_put = None
        self.before_delete = None
        self.bad_get = False
        self.move_fail = False
        self.extra_xml = ''
        self.no_etag = False

    def add(self, name, data=b'hello', age=0):
        self.files[name] = data
        self.modified[name] = time.time() - age * 86400

    def etag(self, name):
        return '"' + hashlib.sha256(self.files[name]).hexdigest() + '"'

    def __call__(self, request):
        name = unquote(request.url.path)
        # request.url.path is already decoded by httpx; percent literal test uses raw_path below.
        name = unquote(request.url.raw_path.decode().split('?')[0])
        self.requests.append((request.method, name, dict(request.headers)))
        def response(code, data=b'', headers=None):
            return httpx.Response(code, content=data, headers=headers, request=request)
        if request.headers.get('authorization') != 'Basic dXNlcjpwYXNz': return response(401)
        if request.method == 'PROPFIND':
            root = name if name in self.files else name.rstrip('/') + '/'
            if root not in self.dirs and root not in self.files: return response(404)
            names = [root]
            if request.headers.get('depth') == '1':
                names += [p for p in [*self.dirs, *self.files] if p != root and p.rstrip('/').rpartition('/')[0] + '/' == root]
            rows = []
            for p in names:
                folder = p in self.dirs
                props = '<d:resourcetype><d:collection/></d:resourcetype>' if folder else f'<d:resourcetype/><d:getcontentlength>{len(self.files[p])}</d:getcontentlength>'
                if not folder and not self.no_etag: props += '<d:getetag>' + escape(self.etag(p)) + '</d:getetag>'
                props += '<d:getlastmodified>' + email.utils.formatdate(self.modified.get(p, time.time()), usegmt=True) + '</d:getlastmodified>'
                rows.append('<d:response><d:href>' + escape(quote(p, safe='/')) + '</d:href><d:propstat><d:prop>' + props + '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            return response(207, ('<d:multistatus xmlns:d="DAV:">' + ''.join(rows) + self.extra_xml + '</d:multistatus>').encode())
        if request.method == 'MKCOL':
            name = name.rstrip('/') + '/'
            if name in self.dirs: return response(405)
            if name.rstrip('/').rpartition('/')[0] + '/' not in self.dirs: return response(409)
            self.dirs.add(name); return response(201)
        if request.method == 'PUT':
            if name in self.files: return response(412)
            self.add(name, request.read())
            if self.after_put: self.after_put()
            return response(201)
        if request.method == 'MOVE':
            if getattr(self, 'reject_public_destination', False) and urlsplit(request.headers['destination']).netloc:
                return response(502)
            target = unquote(urlsplit(request.headers['destination']).path)
            if self.move_fail: return response(500)
            assert request.headers['overwrite'] == 'F'
            if target in self.files or target.rstrip('/') + '/' in self.dirs: return response(412)
            self.add(target, self.files.pop(name)); return response(201)
        if request.method == 'GET':
            if name not in self.files: return response(404)
            if request.headers.get('if-match') and request.headers['if-match'] != self.etag(name): return response(412)
            data = b'x' * len(self.files[name]) if self.bad_get else self.files[name]
            return response(200, data, {'content-length': str(len(data))})
        if request.method == 'DELETE':
            if self.before_delete: self.before_delete(name)
            if name not in self.files: return response(404)
            if request.headers.get('if-match') and request.headers['if-match'] != self.etag(name): return response(412)
            self.files.pop(name); return response(204)
        raise AssertionError(request.method)


class Job:
    def __init__(self, service, **params):
        self.params = {'revision': service.config().get('revision'), **params}
    def check_cancelled(self): pass
    def progress(self, *args): pass


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    dav = Dav()
    original = relay.Remote
    def create(config):
        remote = original(config)
        remote.client.close()
        remote.client = httpx.Client(transport=httpx.MockTransport(dav), auth=('user', 'pass'))
        return remote
    monkeypatch.setattr(relay, 'Remote', create)
    app = App(tmp_path / 'data', register_features=False, register_live=False)
    relay.register(app)
    relay.atomic_json(app.data_dir / 'webdav.json', {'url': 'http://127.0.0.1/dav/', 'username': 'user', 'password_dpapi': relay.protect('pass'), 'remote_path': 'WinToolbox'})
    app.call('relay.save', {'download_dir': str(tmp_path / 'downloads')})
    try: yield app, app.relay, dav
    finally: app.close()


def finish(app, record):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = app.jobs.get(record['id'])
        if result['status'] not in ('queued', 'running', 'cancelling'):
            assert result['status'] == 'completed', result
            return result['result']
        time.sleep(.02)
    raise AssertionError('timeout')


def test_shared_connection_derives_relay_path_from_global_root(fixture):
    app, service, _ = fixture
    old = json.loads(service.path.read_text('utf-8'))
    old.pop('use_shared', None)
    relay.atomic_json(service.path, old)
    shared = {'url': 'https://shared.example/dav/', 'username': 'shared-user',
              'password_dpapi': relay.protect('shared-secret'), 'remote_path': 'backups'}
    relay.atomic_json(app.data_dir / 'webdav.json', shared)
    result = service.get()
    assert result['use_shared'] and result['configured'] and result['remote_path'] == 'backups/file-relay'
    assert result['url'] == shared['url'] and result['username'] == 'shared-user'
    assert 'password_dpapi' not in result
    service.save({'use_shared': True, 'remote_path': '共享/中转'})
    saved = json.loads(service.path.read_text('utf-8'))
    assert not any(k in saved for k in ('password_dpapi', 'url', 'username'))
    before = service.config()['revision']
    relay.atomic_json(app.data_dir / 'webdav.json', {**shared, 'password_dpapi': relay.protect('changed')})
    assert service.config()['revision'] != before
    with pytest.raises(ValueError, match='配置已变化'):
        with service.remote(SimpleNamespace(params={'revision': before})):
            pass


def test_shared_missing_does_not_fallback_to_old_credentials(fixture):
    app, service, _ = fixture
    (app.data_dir / 'webdav.json').unlink()
    result = service.save({'use_shared': False, 'remote_path': 'inbox', 'url': 'https://ignored.invalid/', 'password': 'ignored'})
    assert not result['configured'] and not result['has_password'] and result['url'] == ''
    assert result['remote_path'] == 'WinToolbox/file-relay'


def test_background_upload_browse_download_unicode_zero_bytes(fixture, tmp_path):
    app, service, dav = fixture
    folder = tmp_path / '资料'; folder.mkdir()
    (folder / '中文 #%.txt').write_text('你好', encoding='utf-8')
    (folder / '空文件').write_bytes(b'')
    (folder / '子目录').mkdir()
    result = finish(app, app.call('relay.upload', {'paths': [str(folder)]}))
    assert len(result['uploaded']) == 2 and not result['errors']
    entries = finish(app, app.call('relay.list', {'path': '资料'}))['entries']
    assert {v['name'] for v in entries} == {'中文 #%.txt', '空文件', '子目录'}
    downloaded = finish(app, app.call('relay.download', {'paths': ['资料']}))
    assert len(downloaded['paths']) == 2
    assert (tmp_path / 'downloads/资料/中文 #%.txt').read_text('utf-8') == '你好'
    assert not any('.part' in p for p in dav.files)


def test_same_name_never_overwrites_remote_or_local(fixture, tmp_path):
    app, service, dav = fixture
    source = tmp_path / 'a.txt'; source.write_bytes(b'new')
    dav.add('/dav/WinToolbox/file-relay/a.txt', b'old')
    result = service.run('upload', Job(service, paths=[str(source)], move=True))
    assert result['errors'] and source.read_bytes() == b'new'
    assert dav.files['/dav/WinToolbox/file-relay/a.txt'] == b'old'
    download = tmp_path / 'downloads'; download.mkdir(); (download / 'a.txt').write_bytes(b'local')
    result = service.run('download', Job(service, paths=['a.txt']))
    assert (download / 'a.txt').read_bytes() == b'local'
    assert Path(result['paths'][0]).read_bytes() == b'old'


def test_move_only_after_get_hash_verification(fixture, tmp_path):
    _, service, dav = fixture
    source = tmp_path / 'file.txt'; source.write_bytes(b'important')
    result = service.run('upload', Job(service, paths=[str(source)], move=True))
    assert not source.exists() and result['moved'] == [str(source)]
    assert dav.files['/dav/WinToolbox/file-relay/file.txt'] == b'important'


def test_failed_verification_preserves_source(fixture, tmp_path):
    _, service, dav = fixture
    dav.bad_get = True
    source = tmp_path / 'file.txt'; source.write_bytes(b'important')
    result = service.run('upload', Job(service, paths=[str(source)], move=True))
    assert result['errors'] and not result['moved'] and source.exists()


def test_fnos_proxy_move_falls_back_without_overwrite(fixture, tmp_path):
    _, service, dav = fixture
    dav.reject_public_destination = True
    source = tmp_path / 'proxy-test.txt'
    source.write_bytes(b'proxy-test')
    result = service.run('upload', Job(service, paths=[str(source)]))
    assert result['uploaded'] == ['proxy-test.txt'] and not result['errors']
    assert dav.files['/dav/WinToolbox/file-relay/proxy-test.txt'] == b'proxy-test'
    result = service.run('upload', Job(service, paths=[str(source)]))
    assert result['errors'] and dav.files['/dav/WinToolbox/file-relay/proxy-test.txt'] == b'proxy-test'


def test_source_edit_during_upload_not_published(fixture, tmp_path):
    _, service, dav = fixture
    source = tmp_path / 'file.txt'; source.write_bytes(b'old')
    dav.after_put = lambda: source.write_bytes(b'new')
    result = service.run('upload', Job(service, paths=[str(source)]))
    assert result['errors'] and not dav.files and source.read_bytes() == b'new'


def test_move_failure_cleans_partial_object_and_keeps_source(fixture, tmp_path):
    _, service, dav = fixture
    dav.move_fail = True
    source = tmp_path / 'file.txt'; source.write_bytes(b'old')
    result = service.run('upload', Job(service, paths=[str(source)], move=True))
    assert result['errors'] and not dav.files and source.exists()


def test_cleanup_recursive_age_scope_and_preview_confirmation(fixture):
    _, service, dav = fixture
    dav.dirs.add('/dav/WinToolbox/file-relay/子目录/')
    dav.add('/dav/WinToolbox/file-relay/old', age=9)
    dav.add('/dav/WinToolbox/file-relay/new', age=1)
    dav.add('/dav/WinToolbox/file-relay/子目录/older', age=30)
    dav.add('/dav/outside', age=30)
    plan = service.run('cleanup_preview', Job(service, days=7))
    assert len(plan['files']) == 2
    assert len(dav.files) == 4
    result = service.run('cleanup', Job(service, token=plan['token']))
    assert len(result['removed']) == 2
    assert '/dav/WinToolbox/file-relay/new' in dav.files and '/dav/outside' in dav.files
    assert '/dav/WinToolbox/file-relay/子目录/' in dav.dirs
    with pytest.raises(ValueError, match='失效'): service.run('cleanup', Job(service, token=plan['token']))


def test_cleanup_skips_concurrent_edit_before_check_and_delete(fixture):
    _, service, dav = fixture
    dav.add('/dav/WinToolbox/file-relay/old', age=9)
    plan = service.run('cleanup_preview', Job(service, days=7))
    dav.add('/dav/WinToolbox/file-relay/old', b'changed', age=9)
    result = service.run('cleanup', Job(service, token=plan['token']))
    assert result['skipped'] == ['old']
    plan = service.run('cleanup_preview', Job(service, days=7))
    dav.before_delete = lambda name: dav.add(name, b'newer')
    result = service.run('cleanup', Job(service, token=plan['token']))
    assert result['skipped'] == ['old'] and dav.files['/dav/WinToolbox/file-relay/old'] == b'newer'


def test_cleanup_without_etag_is_explicitly_skipped(fixture):
    _, service, dav = fixture
    dav.no_etag = True
    dav.add('/dav/WinToolbox/file-relay/old', age=9)
    plan = service.run('cleanup_preview', Job(service, days=7))
    assert not plan['files'] and plan['skipped'] == 1


def test_expired_plan_and_changed_config_reject(fixture):
    _, service, _ = fixture
    plan = service.run('cleanup_preview', Job(service, days=7))
    service.plans[plan['token']]['expires'] = 0
    with pytest.raises(ValueError): service.run('cleanup', Job(service, token=plan['token']))
    queued = Job(service)
    shared = json.loads((service.app.data_dir / 'webdav.json').read_text('utf-8'))
    relay.atomic_json(service.app.data_dir / 'webdav.json', {**shared, 'remote_path': '另一个目录'})
    with pytest.raises(ValueError, match='变化'): service.run('list', queued)


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'x/../../bad', 'x\\y', 'a//b', 'x/CON.txt', 'x/file:stream', 'x/trailing.'])
def test_reject_path_escape(path):
    with pytest.raises(ValueError): relay.relative(path)


def test_credential_redaction_and_reset(fixture):
    app, service, _ = fixture
    public = service.get()
    assert public['has_password'] and 'password_dpapi' not in public and 'password' not in public
    record = app.call('relay.list', {'password': 'do-not-log'})
    assert 'do-not-log' not in json.dumps(record)
    finish(app, record)
    with pytest.raises(ValueError, match='凭据'):
        app.jobs.submit('relay.upload', {'password': 'must-not-log'})
    assert service.save({'username': 'someone-else'})['has_password']  # Private overrides are ignored.


def test_shell_enqueue_copies_to_root_and_tracks_job(fixture, tmp_path):
    app, service, dav = fixture
    file = tmp_path / 'shell.txt'; file.write_text('data')
    record = app.call('relay.enqueue', {'paths': [str(file)], 'move': True, 'path': 'outside'})
    assert service.pending({}) == {'job_id': record['id']}
    assert service.pending({}) is None
    result = finish(app, record)
    assert file.exists() and not result['moved'] and '/dav/WinToolbox/file-relay/shell.txt' in dav.files


def test_registry_command_uses_only_quoted_executable_and_file_argument(monkeypatch, tmp_path):
    registry = {}
    notifications = []
    monkeypatch.setattr(relay, 'notify_shell_associations', lambda: notifications.append(True))
    shortcut = {'enabled': False}
    def fake_shortcut(enabled=None, exe=None):
        if enabled is not None: shortcut['enabled'] = enabled
        return shortcut['enabled']
    monkeypatch.setattr(relay, 'sendto_shortcut', fake_shortcut)
    class Key:
        def __init__(self, name): self.name = name
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def create(root, name): registry.setdefault(name, {}); return Key(name)
    def open_key(root, name):
        if name not in registry: raise FileNotFoundError()
        return Key(name)
    fake = SimpleNamespace(HKEY_CURRENT_USER=1, REG_SZ=1, CreateKey=create, OpenKey=open_key,
        SetValueEx=lambda key, name, _, kind, value: registry[key.name].update({name: value}),
        QueryValueEx=lambda key, name: (registry[key.name][name], 1), DeleteKey=lambda root, name: registry.pop(name))
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    exe = tmp_path / 'tool box.exe'; exe.write_bytes(b'fixture')
    monkeypatch.setenv('WINTOOLBOX_APP_EXE', str(exe))
    assert relay.context_menu(True)['enabled']
    assert shortcut['enabled']
    for root in relay.REGISTRY_ROOTS:
        assert registry[root + r'\command'][''] == f'"{exe}" --relay-upload "%1"'
        assert registry[root]['MUIVerb'] == '发送到文件中转站'
        assert registry[root]['Position'] == 'Top'
    assert not relay.context_menu(False)['enabled'] and not registry
    assert not shortcut['enabled']
    assert len(notifications) == 2


def test_sendto_shortcut_is_created_and_removed_in_fixture_only(monkeypatch, tmp_path):
    link = tmp_path / '中文目录/sendto/文件中转站.lnk'
    exe = tmp_path / "tool box's 工具.exe"; exe.write_bytes(b'fixture')
    monkeypatch.setattr(relay, 'sendto_path', lambda: link)
    assert relay.sendto_shortcut(True, exe)
    assert link.stat().st_size > 0
    assert '--relay-upload'.encode('utf-16-le') in link.read_bytes()
    assert exe.name.encode('utf-16-le') in link.read_bytes()
    assert not relay.sendto_shortcut(False, exe)


def test_menu_startup_repairs_and_respects_manual_disable(monkeypatch, tmp_path):
    service = relay.Relay(SimpleNamespace(data_dir=tmp_path))
    state = {'enabled': False, 'supported': True, 'send_to_enabled': False}
    changes = []
    def menu(enabled=None):
        if enabled is not None:
            changes.append(enabled)
            state.update(enabled=enabled, send_to_enabled=enabled)
        return dict(state)
    monkeypatch.setattr(relay, 'context_menu', menu)
    monkeypatch.setenv('WINTOOLBOX_APP_EXE', str(tmp_path / 'first.exe'))
    service.initialize_menu({})
    service.initialize_menu({})
    assert changes == [True]
    state['send_to_enabled'] = False
    service.initialize_menu({})
    assert changes == [True, True]
    monkeypatch.setenv('WINTOOLBOX_APP_EXE', str(tmp_path / 'moved.exe'))
    service.initialize_menu({})
    assert changes == [True, True, True]
    service.menu({'enabled': False})
    service.initialize_menu({})
    assert changes == [True, True, True, False]
    service.menu({'enabled': True})
    assert service.initialize_menu({})['enabled']


def test_failed_menu_registration_is_retried(monkeypatch, tmp_path):
    service = relay.Relay(SimpleNamespace(data_dir=tmp_path))
    def fail(enabled=None):
        if enabled: raise OSError('fixture denied')
        return {'enabled': False, 'supported': True}
    monkeypatch.setattr(relay, 'context_menu', fail)
    with pytest.raises(OSError): service.initialize_menu({})
    assert not (tmp_path / 'relay-menu.json').exists()


def test_real_http_stream_upload_and_download(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    dav = Dav()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def dispatch(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            request = httpx.Request(self.command, f'http://127.0.0.1{self.path}', headers=dict(self.headers), content=body)
            response = dav(request)
            self.send_response(response.status_code)
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            if response.content: self.wfile.write(response.content)
        do_PROPFIND = do_PUT = do_MOVE = do_GET = do_DELETE = do_MKCOL = dispatch
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    app = App(tmp_path / 'data', register_features=False, register_live=False)
    try:
        relay.register(app)
        relay.atomic_json(app.data_dir / 'webdav.json', {'url': f'http://127.0.0.1:{server.server_port}/dav/', 'username': 'user', 'password_dpapi': relay.protect('pass'), 'remote_path': 'WinToolbox'})
        app.call('relay.save', {'download_dir': str(tmp_path / 'downloads')})
        source = tmp_path / 'large.bin'; source.write_bytes(bytes(range(256)) * 10000)
        result = finish(app, app.call('relay.upload', {'paths': [str(source)]}))
        assert not result['errors'] and result['uploaded'] == ['large.bin']
        result = finish(app, app.call('relay.download', {'paths': ['large.bin']}))
        assert Path(result['paths'][0]).read_bytes() == source.read_bytes()
    finally:
        app.close(); server.shutdown(); server.server_close(); worker.join(timeout=2)


def test_only_same_url_trailing_slash_redirect_is_followed(fixture):
    _, service, _ = fixture
    remote = relay.Remote(service.config())
    requests = []
    def handle(request):
        requests.append(str(request.url))
        if len(requests) == 1: return httpx.Response(301, headers={'Location': str(request.url) + '/'})
        return httpx.Response(207, content=b'<d:multistatus xmlns:d="DAV:"/>')
    remote.client.close()
    remote.client = httpx.Client(transport=httpx.MockTransport(handle))
    try:
        with remote.propfind(remote.url('folder'), 0) as response: assert response.status_code == 207
        assert len(requests) == 2
        remote.client.close()
        remote.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={'Location': 'http://attacker.invalid/dav/'})))
        with remote.propfind(remote.url('folder'), 0) as response: assert response.status_code == 302
    finally: remote.close()


def test_empty_directory_download_is_preserved(fixture, tmp_path):
    _, service, dav = fixture
    dav.dirs.add('/dav/WinToolbox/file-relay/empty/')
    result = service.run('download', Job(service, paths=['empty']))
    assert not result['paths'] and (tmp_path / 'downloads/empty').is_dir()


def test_selected_delete_preserves_changed_and_unselected_files(fixture):
    _, service, dav = fixture
    for name in ('selected.txt', 'changed.txt', 'keep.txt'): dav.add('/dav/WinToolbox/file-relay/' + name)
    listing = service.run('list', Job(service, path=''))['entries']
    selected = [row for row in listing if row['name'] != 'keep.txt']
    dav.add('/dav/WinToolbox/file-relay/changed.txt', b'new content')
    result = service.run('delete', Job(service, entries=selected))
    assert result['removed'] == ['selected.txt']
    assert result['skipped'] == ['changed.txt']
    assert '/dav/WinToolbox/file-relay/keep.txt' in dav.files and '/dav/WinToolbox/file-relay/changed.txt' in dav.files


def test_selected_delete_validates_whole_batch_before_mutation(fixture):
    _, service, dav = fixture
    dav.add('/dav/WinToolbox/file-relay/keep.txt')
    entry = service.run('list', Job(service, path=''))['entries'][0]
    with pytest.raises(ValueError): service.run('delete', Job(service, entries=[entry, {'path': '../outside'}]))
    assert '/dav/WinToolbox/file-relay/keep.txt' in dav.files
    with pytest.raises(ValueError): service.run('delete', Job(service, entries=[{**entry, 'directory': True}]))
    with pytest.raises(ValueError): service.run('delete', Job(service, entries=[{**entry, 'etag': ''}]))


def test_drag_cache_preserves_names_and_empty_directories(fixture, tmp_path):
    app, service, dav = fixture
    dav.dirs.add('/dav/WinToolbox/file-relay/folder/')
    dav.dirs.add('/dav/WinToolbox/file-relay/folder/empty/')
    dav.add('/dav/WinToolbox/file-relay/folder/中文.txt', b'drag fixture')
    result = finish(app, app.call('relay.prepare_drag', {'paths': ['folder']}))
    cached = Path(result['paths'][0])
    assert cached.is_relative_to(app.data_dir / 'relay-drag-cache')
    assert cached.name == 'folder'
    assert (cached / '中文.txt').read_bytes() == b'drag fixture'
    assert (cached / 'empty').is_dir()
    assert not (tmp_path / 'downloads/folder').exists()
    assert dav.files['/dav/WinToolbox/file-relay/folder/中文.txt'] == b'drag fixture'


def test_connection_edit_fails_fast_during_mutation(fixture):
    _, service, _ = fixture
    with service.write_lock:
        with pytest.raises(ValueError, match='正在进行'):
            service.save({'remote_path': 'different'})
    assert service.config()['remote_path'] == 'WinToolbox/file-relay'


def test_fnos_existing_collection_with_missing_optional_properties(fixture):
    _, service, _ = fixture
    remote = relay.Remote(service.config())
    remote.client.close()
    xml = '<D:multistatus xmlns:D="DAV:"><D:response><D:href>/dav/WinToolbox/file-relay/</D:href><D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype><D:getlastmodified>Thu, 24 Sep 2026 03:17:45 GMT</D:getlastmodified></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat><D:propstat><D:prop><D:getcontentlength/><D:getetag/></D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat></D:response></D:multistatus>'
    methods = []
    def response(request):
        methods.append(request.method)
        return httpx.Response(207, content=xml.encode())
    remote.client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        remote.ensure('')
        assert remote.listing('') == []
        assert methods and set(methods) == {'PROPFIND'}  # Existing roots need no MKCOL.
    finally: remote.close()
