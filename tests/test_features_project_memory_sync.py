"""Actual HTTP DAV replication with partial project availability and offline conflicts."""
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest

from toolbox.features import webdav
from toolbox.project_memory.store import MemoryStore
from toolbox.project_memory_sync import MemoryRemote, canonical, digest
from toolbox.project_memory_ssh import target, INSTALL, EXCHANGE


class Job:
    def check_cancelled(self):
        pass
    def progress(self, *args):
        pass


@pytest.fixture
def dav(monkeypatch):
    monkeypatch.setattr(webdav, 'protect', lambda value, **kwargs: value)
    state = {'dirs': {'/dav/', '/dav/toolbox/'}, 'files': {}, 'evil': False}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        def log_message(self, *args):
            pass
        def respond(self, status, body=b''):
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def path_value(self):
            return unquote(urlsplit(self.path).path)
        def do_MKCOL(self):
            path = self.path_value()
            if path in state['dirs']:
                return self.respond(405)
            if path.rstrip('/').rpartition('/')[0] + '/' not in state['dirs']:
                return self.respond(409)
            state['dirs'].add(path)
            self.respond(201)
        def do_PROPFIND(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            path = self.path_value()
            if path not in state['dirs']:
                return self.respond(404)
            names = [path]
            if self.headers.get('Depth') == '1':
                names += [p for p in list(state['dirs']) + list(state['files']) if p != path and p.rstrip('/').rpartition('/')[0] + '/' == path]
            rows = []
            for name in names:
                kind = '<d:collection/>' if name in state['dirs'] else ''
                rows.append('<d:response><d:href>' + escape(quote(name, safe='/')) + '</d:href><d:propstat><d:prop><d:resourcetype>' + kind + '</d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            if state['evil']:
                rows.append('<d:response><d:href>https://evil.example/steal.json</d:href><d:propstat><d:prop/><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            self.respond(207, ('<d:multistatus xmlns:d="DAV:">' + ''.join(rows) + '</d:multistatus>').encode())
        def do_PUT(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            path = self.path_value()
            assert self.headers.get('If-None-Match') == '*'
            if path in state['files']:
                return self.respond(412)
            state['files'][path] = body
            self.respond(201)
        def do_GET(self):
            path = self.path_value()
            self.respond(200, state['files'][path]) if path in state['files'] else self.respond(404)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'url': f'http://127.0.0.1:{server.server_port}/dav/', 'remote_path': 'toolbox', 'username': 'fixture', 'password_dpapi': 'fixture'}
    with contextlib.closing(MemoryRemote(config)) as remote:
        yield remote, state
    server.shutdown()
    server.server_close()
    thread.join()


def test_partial_projects_retries_and_conflicts(tmp_path, dav):
    remote, state = dav
    a = MemoryStore(tmp_path / 'a', device_id='a')
    b = MemoryStore(tmp_path / 'b', device_id='b')
    try:
        project = a.create_project('仅在设备甲的项目')
        path = tmp_path / 'project'; path.mkdir()
        a.bind_project(project['id'], str(path))
        blob = a.add_blob(b'attachment')
        entry = a.save_entry({'title': '离线知识', 'body': '原始内容', 'kind': 'knowledge', 'project_id': project['id'], 'attachments': [blob]})
        remote.sync(a, Job())
        assert remote.libraries() == [{'library_id': a.info()['library_id']}]
        b.join_library(a.info()['library_id'])
        remote.sync(b, Job())
        assert len(b.projects()) == 1
        assert b.projects()[0]['locations'] == []
        assert b.entries() == []  # Catalog is browsable, body requires a subscription.
        b.subscribe(project['id'])
        remote.sync(b, Job())
        assert b.get_entry(entry['id'])['body'] == '原始内容'
        assert b.get_blob(blob['hash']) == b'attachment'
        a.save_entry({**entry, 'body': '甲离线修改'}, parents=entry['heads'])
        b.save_entry({**entry, 'body': '乙离线修改'}, parents=entry['heads'])
        remote.sync(a, Job()); remote.sync(b, Job()); remote.sync(a, Job())
        assert len(a.conflicts()) == len(b.conflicts()) == 1
        count = a.info()['operation_count']
        remote.sync(a, Job())
        assert a.info()['operation_count'] == count
        assert not any('locations' in json.loads(body).get('data', {}) for name, body in state['files'].items() if name.endswith('.json'))
    finally:
        a.close(); b.close()


def test_mismatched_library_and_tampered_immutable(tmp_path, dav):
    remote, state = dav
    a = MemoryStore(tmp_path / 'a', device_id='a')
    b = MemoryStore(tmp_path / 'b', device_id='b')
    try:
        a.create_project('甲')
        remote.sync(a, Job())
        with pytest.raises(ValueError, match='其他知识库'):
            remote.sync(b, Job())
        path = next(p for p in state['files'] if '/catalog/' in p)
        state['files'][path] = b'{}'
        with pytest.raises(ValueError, match='不可变记录'):
            remote.sync(a, Job(), verify=True)
        state['evil'] = True
        assert all(name != 'steal.json' for name, _ in remote.children(remote.base))
    finally:
        a.close(); b.close()


@pytest.mark.parametrize('params', [{'host': '-oProxyCommand=x', 'remote_root': '/safe'}, {'host': 'host;whoami', 'remote_root': '/safe'}, {'host': 'host', 'remote_root': '/x/../y'}, {'host': 'host', 'remote_root': '/'}])
def test_ssh_rejects_unsafe_targets(params):
    with pytest.raises(ValueError):
        target(params)


def test_ssh_path_is_data_not_shell_code():
    assert target({'host': 'user@host-alias', 'remote_root': "/home/u/it's data;$(id)"})[1].endswith('$(id)')
    compile(INSTALL, '<install>', 'exec')
    compile(EXCHANGE, '<exchange>', 'exec')


def test_ssh_runtime_install_and_bidirectional_exchange_without_network(tmp_path, monkeypatch):
    """Run the actual remote Python programs locally; transport args separately tested."""
    import subprocess
    import sys
    from toolbox import project_memory_ssh as ssh
    a = MemoryStore(tmp_path / 'a', device_id='a')
    remote_root = tmp_path / 'remote'
    remote_project = tmp_path / 'remote-project'; remote_project.mkdir()
    agents = remote_project / 'AGENTS.md'; agents.write_text('# 保留项目要求\n', encoding='utf-8')
    def local_run(host, code, data, job, timeout=120):
        # Windows test path is carried as JSON, never shell-interpolated.
        data['root'] = str(remote_root)
        if data.get('project_path'):
            data['project_path'] = str(remote_project)
        import os
        env = {**os.environ, 'XDG_STATE_HOME': str(tmp_path / 'state')}
        process = subprocess.run([sys.executable, '-c', code], input=json.dumps(data).encode(), capture_output=True, check=True, env=env)
        return json.loads(process.stdout)
    monkeypatch.setattr(ssh, 'run', local_run)
    try:
        p = a.create_project('SSH 项目')
        e = a.save_entry({'title': '离线记录', 'body': '本地创建', 'kind': 'task', 'project_id': p['id']})
        params = {'host': 'fixture-host', 'remote_root': '/tmp/fixture', 'project_ids': [p['id']], 'project_path': '/fixture/project', 'project_id': p['id']}
        assert ssh.install(params, Job())['installed']
        ssh.sync(params, a, Job())
        initial = agents.read_text(encoding='utf-8')
        assert initial.startswith('# 保留项目要求\n')
        assert 'agent-knowledge.py" recall --project .' in initial
        # Remote Codex can capture and recall with the installed launcher, without skills.
        launcher = tmp_path / 'state' / 'wintoolbox-agent' / 'agent-knowledge.py'
        captured = subprocess.run([sys.executable, str(launcher), 'capture', '--project', '.', '--title', '远端独立捕获',
                                   '--body', '电脑离线后记录', '--kind', 'knowledge'], cwd=remote_project,
                                  capture_output=True, check=True)
        assert json.loads(captured.stdout)['title'] == '远端独立捕获'
        recalled = subprocess.run([sys.executable, str(launcher), 'recall', '--project', '.', '--query', '远端独立捕获'],
                                 cwd=remote_project, capture_output=True, check=True)
        assert '远端独立捕获' in recalled.stdout.decode('utf-8')
        customized = initial.replace('## Agent Knowledge', '## 自定义项目记忆要求')
        agents.write_text(customized, encoding='utf-8')
        r = MemoryStore(remote_root / 'data', device_id='remote-fixture')
        try:
            assert r.get_entry(e['id'])['body'] == '本地创建'
            r.save_entry({**r.get_entry(e['id']), 'body': '电脑离线时远端继续记录'})
        finally:
            r.close()
        result = ssh.sync(params, a, Job())
        assert result['downloaded'] == 2
        assert agents.read_text(encoding='utf-8') == customized
        assert a.get_entry(e['id'])['body'] == '电脑离线时远端继续记录'
    finally:
        a.close()


def test_rpc_local_attachment_and_restore_lifecycle(tmp_path):
    from types import SimpleNamespace
    from toolbox.features import project_memory
    class Jobs:
        def register(self, name, runner):
            pass
    app = SimpleNamespace(data_dir=tmp_path, jobs=Jobs(), data_lock=threading.RLock(), maintenance=False)
    handlers = {}
    app.register = lambda name, handler: handlers.setdefault(name, handler)
    project_memory.register(app)
    try:
        project = handlers['memory.project.create']({'name': '本机项目'})
        with pytest.raises(ValueError, match='不存在'):
            handlers['memory.project.bind']({'project_id': project['id'], 'path': str(tmp_path / 'missing')})
        source = tmp_path / '附件.txt'; source.write_text('附件中文', encoding='utf-8')
        blob = handlers['memory.attachment.add']({'path': str(source)})
        exported = handlers['memory.attachment.path'](blob)
        from pathlib import Path
        assert Path(exported['path']).read_text(encoding='utf-8') == '附件中文'
        with pytest.raises(ValueError, match='文件名'):
            handlers['memory.attachment.path']({**blob, 'name': '../escape'})
        configured = handlers['memory.sync.configure']({'auto_sync': True, 'interval_seconds': 1})
        assert configured['auto_sync'] and configured['interval_seconds'] == 60
        app.memory_before_restore(); app.memory_after_restore()
        assert handlers['memory.projects']({})['projects'][0]['id'] == project['id']
    finally:
        app.memory_close()


def test_saved_ssh_targets_are_private_and_offline_host_does_not_block_dav(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from toolbox.features import project_memory
    runners = {}
    jobs = SimpleNamespace(register=lambda name, run: runners.setdefault(name, run))
    app = SimpleNamespace(data_dir=tmp_path, jobs=jobs, data_lock=threading.RLock(), maintenance=False)
    handlers = {}; app.register = lambda name, handler: handlers.setdefault(name, handler)
    project_memory.register(app)
    app.memory_stop()
    order = []
    class DAV:
        def __init__(self, config):
            pass
        def sync(self, store, job, verify=False):
            order.append('dav')
            return {'uploaded': 1, 'downloaded': 0}
        def close(self):
            pass
    def offline(params, store, job):
        order.append('ssh')
        raise TimeoutError('fixture host offline')
    monkeypatch.setattr(project_memory, 'MemoryRemote', DAV)
    monkeypatch.setattr(project_memory.project_memory_ssh, 'sync', offline)
    try:
        project = handlers['memory.project.create']({'name': '远端项目'})
        params = {'host': 'fixture-host', 'remote_root': '/home/u/.local/share/wintoolbox-agent',
                  'project_ids': [project['id']], 'project_path': '/home/u/private/project', 'project_id': project['id'], 'enabled': True}
        target = handlers['memory.ssh.configure'](params)['target']
        assert handlers['memory.ssh.configure'](params)['target']['id'] == target['id']
        assert handlers['memory.ssh.targets']({})['targets'] == [target]
        serialized = json.dumps(app.project_memory.export_operations())
        assert 'fixture-host' not in serialized and '/home/u' not in serialized
        (tmp_path / 'webdav.json').write_text(json.dumps({'url': 'https://fixture.invalid', 'username': 'fixture', 'password_dpapi': 'fixture'}))
        job = Job(); job.params = {}
        result = runners['memory.sync.run'](job)
        assert order == ['ssh', 'dav']
        assert result['uploaded'] == 1 and result['warnings'][0]['host'] == 'fixture-host'
        assert handlers['memory.sync.status']({})['last_result']['warnings']
        handlers['memory.ssh.remove']({'id': target['id']})
        assert handlers['memory.ssh.targets']({}) == {'targets': []}
    finally:
        app.memory_close()


def test_saved_ssh_only_sync_works_without_webdav(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from toolbox.features import project_memory
    runners = {}; handlers = {}
    app = SimpleNamespace(data_dir=tmp_path, data_lock=threading.RLock(), maintenance=False,
                          jobs=SimpleNamespace(register=lambda name, run: runners.setdefault(name, run)))
    app.register = lambda name, handler: handlers.setdefault(name, handler)
    project_memory.register(app); app.memory_stop()
    monkeypatch.setattr(project_memory.project_memory_ssh, 'sync', lambda params, store, job: {'uploaded': 2, 'downloaded': 3})
    try:
        handlers['memory.ssh.configure']({'host': 'fixture', 'remote_root': '/data/agent', 'project_ids': []})
        assert handlers['memory.sync.status']({})['configured']
        job = Job(); job.params = {}
        result = runners['memory.sync.run'](job)
        assert result['ssh'][0]['downloaded'] == 3
        assert result['webdav'] == 'not_configured'
    finally:
        app.memory_close()
