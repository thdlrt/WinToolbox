"""WebDAV integration against a local authenticated HTTP server; no user data."""
import base64
import contextlib
import email.utils
import hashlib
import json
import os
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest

from toolbox.app import App
from toolbox.features import backups, webdav
from toolbox.jobs import Cancelled


def finish(app, job, status='completed'):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        row = app.jobs.get(job['id'])
        if row['status'] not in ('queued', 'running', 'cancelling') and job['id'] not in app.jobs.active:
            assert row['status'] == status, row
            return row
        time.sleep(.02)
    raise AssertionError('WebDAV fixture timed out')


def filename(encrypted=False):
    return 'wintoolbox-20260910T120000Z-' + uuid.uuid4().hex + ('.enc' if encrypted else '') + '.wtbak'


@pytest.fixture
def server():
    state = {'dirs': {'/dav/', '/dav/重要文件/', '/dav/重要文件/sync/'}, 'files': {}, 'methods': [],
             'auth': 'Basic ' + base64.b64encode(b'fixture-user:fixture-dav-password').decode(), 'move_fail': False, 'redirect': None}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def respond(self, status, body=b'', headers=None):
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

        def authorized(self):
            self.name = unquote(urlsplit(self.path).path)
            state['methods'].append((self.command, self.name))
            if state['redirect'] == self.command:
                self.respond(302, headers={'Location': 'http://127.0.0.1:1/credential-sink'})
                return False
            if self.headers.get('Authorization') != state['auth']:
                self.respond(401)
                return False
            return True

        def do_PROPFIND(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if not self.authorized():
                return
            if self.name not in state['dirs']:
                self.respond(404)
                return
            names = [self.name]
            if self.headers.get('Depth') == '1':
                names.extend(name for name in [*state['dirs'], *state['files']] if name != self.name and name.rsplit('/', 1)[0] + '/' == self.name)
            responses = []
            for name in names:
                props = '<d:resourcetype><d:collection/></d:resourcetype>' if name in state['dirs'] else '<d:resourcetype/><d:getcontentlength>' + str(len(state['files'][name])) + '</d:getcontentlength>'
                responses.append('<d:response><d:href>' + escape(quote(name, safe='/')) + '</d:href><d:propstat><d:prop>' + props + '<d:getlastmodified>' + email.utils.formatdate(usegmt=True) + '</d:getlastmodified></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            self.respond(207, ('<d:multistatus xmlns:d="DAV:">' + ''.join(responses) + '</d:multistatus>').encode(), {'Content-Type': 'application/xml'})

        def do_MKCOL(self):
            if not self.authorized():
                return
            if self.name in state['dirs']:
                self.respond(405)
            elif self.name.rsplit('/', 2)[0] + '/' not in state['dirs']:
                self.respond(409)
            else:
                state['dirs'].add(self.name)
                self.respond(201)

        def do_PUT(self):
            if not self.authorized():
                return
            if self.name in state['files'] and self.headers.get('If-None-Match') == '*':
                self.respond(412)
                return
            data = self.rfile.read(int(self.headers['Content-Length']))
            state['files'][self.name] = data
            self.respond(201)

        def do_MOVE(self):
            if not self.authorized():
                return
            destination = unquote(urlsplit(self.headers['Destination']).path)
            if state['move_fail']:
                self.respond(501)
            elif destination in state['files'] and self.headers.get('Overwrite') == 'F':
                self.respond(412)
            elif self.name not in state['files']:
                self.respond(404)
            else:
                state['files'][destination] = state['files'].pop(self.name)
                self.respond(201)

        def do_GET(self):
            if self.authorized():
                self.respond(200, state['files'][self.name]) if self.name in state['files'] else self.respond(404)

        def do_DELETE(self):
            if self.authorized():
                state['files'].pop(self.name, None)
                self.respond(204)

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    state['url'] = f'http://127.0.0.1:{http.server_port}/dav/'
    yield state
    http.shutdown()
    http.server_close()
    thread.join(timeout=2)


@pytest.fixture
def pair(tmp_path, monkeypatch, server):
    def fixture_protect(value, decrypt=False):
        return base64.b64decode(value).decode() if decrypt else base64.b64encode(value.encode()).decode()
    monkeypatch.setattr(webdav, 'protect', fixture_protect)
    monkeypatch.setattr('toolbox.settings.protect', fixture_protect)
    apps = [App(tmp_path / name, register_live=False) for name in ('source', 'target')]
    for app in apps:
        app.call('webdav.save', {'url': server['url'], 'remote_path': '重要文件/sync/WinToolbox',
                                'username': 'fixture-user', 'password': 'fixture-dav-password'})
    yield (*apps, server)
    for app in apps:
        app.close()
        app.jobs.pool.shutdown(wait=True)


def test_config_redaction_normalization_and_password_retention(pair):
    app, _, server = pair
    public = app.call('webdav.get')
    assert public['configured'] and public['has_password'] and public['include_media']
    assert not public['include_models'] and not public['include_secrets']
    assert 'fixture-dav-password' not in json.dumps(public)
    assert 'fixture-dav-password' not in (app.data_dir / 'webdav.json').read_text('utf-8')
    assert app.call('webdav.save', {'password': ''})['has_password']
    assert not app.call('webdav.save', {'username': 'new-user'})['has_password']
    assert webdav.normalized_url('https://example.com:443/dav') == 'https://example.com/dav/'
    assert webdav.normalized_url('http://localhost:80/dav') == 'http://localhost/dav/'
    assert webdav.normalized_url('https://example.com:8443/dav') == 'https://example.com:8443/dav/'
    for url in ('http://example.com/dav', 'https://name:password@example.com/', 'https://example.com/?token=secret', 'https://example.com/../'):
        with pytest.raises(ValueError):
            app.call('webdav.save', {'url': url})
    for path in ('../escape', 'parent//child', 'parent/%2e%2e/child', 'parent\\child'):
        with pytest.raises(ValueError):
            app.call('webdav.save', {'remote_path': path})


def test_only_last_directory_created_and_redirects_rejected(pair):
    app, _, server = pair
    assert app.call('webdav.test')['ok']
    assert ('MKCOL', '/dav/重要文件/sync/WinToolbox/') in server['methods']
    assert app.call('webdav.test')['ok']
    app.call('webdav.save', {'remote_path': 'not-created/WinToolbox'})
    with pytest.raises(ValueError, match='父文件夹不存在'):
        app.call('webdav.test')
    assert '/dav/not-created/' not in server['dirs']
    server['redirect'] = 'PROPFIND'
    with pytest.raises(ValueError, match='跳转'):
        app.call('webdav.test')


def test_encrypted_roundtrip_preserves_media_rebases_paths_and_saves_recovery(pair):
    source, target, server = pair
    source.call('settings.update', {'preferences': {'fixture': 'remote-value'}})
    source.call('settings.update', {'providers': [{'id': 'fixture', 'kind': 'openai', 'name': 'fixture', 'base_url': 'https://example.invalid/v1', 'api_key': 'fixture-api-key'}]})
    practice = source.data_dir / 'practice'
    (practice / 'audio' / 'fixture.wav').write_bytes(b'RIFF-fixture-audio')
    (practice / 'items' / ('a' * 64 + '.json')).write_text(json.dumps({'id': 'a' * 64, 'path': str(practice / 'audio' / 'fixture.wav')}))
    target.call('settings.update', {'preferences': {'fixture': 'local-before-restore'}})
    config_before = (target.data_dir / 'webdav.json').read_bytes()
    uploaded = finish(source, source.call('webdav.upload', {'backup_password': 'fixture-backup-password', 'include_secrets': True}))['result']
    assert uploaded['encrypted'] and uploaded['name'].endswith('.enc.wtbak')
    assert not any(a['kind'] == 'backup' for a in source.jobs.list()[0]['artifacts'])
    assert not any(name.endswith('.part') for name in server['files'])
    assert target.call('webdav.list')['snapshots'][0]['name'] == uploaded['name']
    restored = finish(target, target.call('webdav.restore', {'name': uploaded['name'], 'backup_password': 'fixture-backup-password'}))['result']
    assert Path(restored['recovery_path']).is_file()
    assert target.settings.get()['preferences']['fixture'] == 'remote-value'
    assert target.settings.secret('fixture') == 'fixture-api-key'
    migrated = json.loads((target.data_dir / 'practice/items' / ('a' * 64 + '.json')).read_text())
    assert migrated['path'].startswith(str(target.data_dir))
    assert (target.data_dir / 'practice/audio/fixture.wav').read_bytes() == b'RIFF-fixture-audio'
    assert (target.data_dir / 'webdav.json').read_bytes() == config_before
    serialized = json.dumps(source.jobs.list() + target.jobs.list())
    assert not any(secret in serialized for secret in ('fixture-backup-password', 'fixture-dav-password', 'fixture-api-key'))


def test_wrong_password_and_tampered_backup_do_not_change_data(pair):
    source, target, server = pair
    target.call('settings.update', {'preferences': {'keep': 'original'}})
    before = target.settings.path.read_bytes()
    uploaded = finish(source, source.call('webdav.upload', {'backup_password': 'correct-password'}))['result']
    finish(target, target.call('webdav.restore', {'name': uploaded['name'], 'backup_password': 'wrong-password'}), 'failed')
    assert target.settings.path.read_bytes() == before
    name = filename()
    server['files']['/dav/重要文件/sync/WinToolbox/' + name] = b'not-a-valid-backup'
    finish(target, target.call('webdav.restore', {'name': name}), 'failed')
    assert target.settings.path.read_bytes() == before
    assert not (target.data_dir.parent / 'target-recovery').exists()


def test_empty_snapshot_propagates_deletions_but_excluded_models_survive(pair):
    source, target, _ = pair
    old = target.data_dir / 'practice/items/old.json'
    old.write_text('old paragraph')
    models = target.data_dir / 'models/keep-model.bin'
    models.write_bytes(b'keep model')
    uploaded = finish(source, source.call('webdav.upload'))['result']
    restored = finish(target, target.call('webdav.restore', {'name': uploaded['name']}))['result']
    assert not old.exists() and models.read_bytes() == b'keep model'
    assert Path(restored['recovery_path']).is_file()
    with zipfile.ZipFile(restored['recovery_path']) as archive:
        assert archive.read('practice/items/old.json') == b'old paragraph'


def test_failed_move_never_lists_incomplete_snapshot(pair):
    app, _, server = pair
    server['move_fail'] = True
    finish(app, app.call('webdav.upload'), 'failed')
    assert app.call('webdav.list')['snapshots'] == []
    assert not server['files']


def test_remote_names_are_restricted_and_generic_job_secrets_rejected(pair):
    app, _, _ = pair
    for name in ('../settings.json', 'https://elsewhere/snapshot.wtbak', 'foreign.wtbak'):
        with pytest.raises(ValueError):
            app.call('webdav.restore', {'name': name})
    before = len(app.jobs.list())
    with pytest.raises(ValueError):
        app.call('jobs.submit', {'tool': 'webdav.upload', 'params': {'backup_password': 'never-write-this-password'}})
    assert len(app.jobs.list()) == before
    assert 'never-write-this-password' not in json.dumps(app.jobs.list())
    with pytest.raises(ValueError, match='8'):
        app.call('webdav.upload', {'include_secrets': True})


def test_cancelled_upload_never_publishes_snapshot(pair, tmp_path):
    app, _, server = pair
    app.call('webdav.test')
    raw = tmp_path / 'upload.bin'
    raw.write_bytes(os.urandom(3 * 1024 * 1024))
    class Job:
        checks = 0
        def check_cancelled(self):
            self.checks += 1
            if self.checks > 1:
                raise Cancelled('fixture cancelled')
        def progress(self, *args):
            pass
    remote = webdav.Remote(json.loads((app.data_dir / 'webdav.json').read_text('utf-8')))
    try:
        with pytest.raises(Cancelled):
            remote.upload(raw, filename(), Job())
        assert remote.listing() == []
    finally:
        remote.close()
    assert not any(webdav.SNAPSHOT.fullmatch(name.rsplit('/', 1)[-1]) for name in server['files'])


@pytest.mark.parametrize('directory', ['webdav/', 'secrets.json/', 'settings.json/'])
def test_restore_rejects_undeclared_or_invalid_empty_directory(pair, tmp_path, directory):
    app, _, _ = pair
    app.settings.update({'preferences': {'keep': 'original'}})
    before = app.settings.path.read_bytes()
    source = tmp_path / 'malicious.wtbak'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('backup-manifest.json', json.dumps({'format': 'WinToolbox backup', 'version': 1, 'data_dir': 'C:/fixture', 'directories': [], 'files': {}}))
        archive.writestr(directory, b'')
    finish(app, app.call('backups.import', {'path': str(source)}), 'failed')
    assert app.settings.path.read_bytes() == before


def test_before_restore_rejection_keeps_existing_secrets(pair, tmp_path, monkeypatch):
    source, target, _ = pair
    for app, key in ((source, 'source-key'), (target, 'target-key')):
        app.settings.update({'providers': [{'id': 'fixture', 'kind': 'openai', 'name': 'fixture', 'base_url': 'https://example.invalid/v1', 'api_key': key}]})
    archive = tmp_path / 'with-secrets.wtbak'
    finish(source, source.call('backups.export', {'path': str(archive), 'include_secrets': True, 'password': 'fixture-password'}))
    before = target.settings.secret_path.read_bytes()
    def reject(_):
        raise RuntimeError('Live session is running')
    monkeypatch.setattr(target, 'before_restore', reject)
    finish(target, target.call('backups.import', {'path': str(archive), 'password': 'fixture-password'}), 'failed')
    assert target.settings.secret_path.read_bytes() == before
    assert target.settings.secret('fixture') == 'target-key'


def test_backup_cannot_replace_models_outside_declared_scope(pair, tmp_path):
    app, _, _ = pair
    target = app.data_dir / 'models/existing.bin'
    target.write_bytes(b'keep-local-model')
    payload = b'out-of-scope-model'
    source = tmp_path / 'invalid-scope.wtbak'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('backup-manifest.json', json.dumps({
            'format': 'WinToolbox backup', 'version': 1, 'data_dir': 'C:/fixture', 'include_models': False,
            'directories': [], 'files': {'models/existing.bin': {'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)}},
        }))
        archive.writestr('models/existing.bin', payload)
    row = finish(app, app.call('backups.import', {'path': str(source)}), 'failed')
    assert '范围' in row['error'] and target.read_bytes() == b'keep-local-model'
