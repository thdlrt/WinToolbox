import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.features import file_locks, quick_menu


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', register_features=False, register_live=False)
    quick_menu.register(value)
    yield value
    value.close()


def finish(app, job):
    for _ in range(500):
        result = app.jobs.get(job['id'])
        if result['status'] not in ('queued', 'running', 'cancelling'):
            return result
        time.sleep(.02)
    raise AssertionError('job timeout')


def test_release_needs_confirmation_and_scanned_owner(app, tmp_path, monkeypatch):
    path = tmp_path / 'a.txt'; path.write_text('keep')
    context = app.call('quick.capture', {'paths': [str(path)]})
    for params in ({}, {'confirm': True}, {'confirm': True, 'mode': 'terminate', 'process': '1:2'}):
        with pytest.raises(ValueError):
            app.call('quick.release', {'id': context['id'], **params})
    assert path.read_text() == 'keep'


def test_reused_pid_is_not_terminated(app, tmp_path, monkeypatch):
    path = tmp_path / 'a.txt'; path.write_text('keep')
    process = {'key': '123:old', 'pid': 123, 'start': 'old', 'can_release': True}
    current = {'processes': [process], 'files': [], 'notice': ''}
    monkeypatch.setattr(file_locks, 'scan', lambda *a: current)
    context = app.call('quick.capture', {'paths': [str(path)]})
    assert finish(app, app.call('quick.run', {'id': context['id'], 'action': 'file_locks'}))['status'] == 'completed'
    current = {'processes': [{**process, 'key': '123:new', 'start': 'new'}], 'files': [], 'notice': ''}
    monkeypatch.setattr(file_locks.WindowsLocks, 'release', lambda *a: pytest.fail('must not terminate reused PID'))
    assert finish(app, app.call('quick.release', {'id': context['id'], 'process': '123:old', 'confirm': True, 'mode': 'terminate'}))['status'] == 'completed'
    assert path.read_text() == 'keep'


def test_unavailable_scan_fails_actionably(app, tmp_path, monkeypatch):
    path = tmp_path / 'a.txt'; path.write_text('keep')
    def fail(*a): raise OSError(5, 'Windows 占用查询失败')
    monkeypatch.setattr(file_locks, 'scan', fail)
    context = app.call('quick.capture', {'paths': [str(path)]})
    result = finish(app, app.call('quick.run', {'id': context['id'], 'action': 'file_locks'}))
    assert result['status'] == 'failed' and '占用查询失败' in result['error']
    assert not app.call('quick.snapshot', {'id': context['id']}).get('file_locks')


@pytest.mark.skipif(os.name != 'nt', reason='real Windows API')
@pytest.mark.parametrize('selection', ['file', 'folder', 'mixed'])
def test_real_lock_end_to_end(app, tmp_path, selection):
    root = tmp_path / 'selected'; root.mkdir()
    nested = root / 'nested'; nested.mkdir()
    path = nested / '中文 locked file.txt'; path.write_text('preserve original')
    code = "from toolbox.features.file_locks import WindowsLocks; import sys,time; a=WindowsLocks(); h=a.kernel.CreateFileW(sys.argv[1],0x80000000,1,None,3,0x80,None); print('ready',flush=True); time.sleep(90)"
    child = subprocess.Popen([sys._base_executable, '-c', code, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW, text=True,
                             env={**os.environ, 'PYTHONPATH': str(Path(file_locks.__file__).parents[2])})
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(PermissionError): path.unlink()
        selected = [str(path)] if selection == 'file' else [str(root)] if selection == 'folder' else [str(root), str(path)]
        context = app.call('quick.capture', {'paths': selected})
        assert 'file_locks' in [a['id'] for a in context['actions']]
        assert finish(app, app.call('quick.run', {'id': context['id'], 'action': 'file_locks'}))['status'] == 'completed'
        snapshot = app.call('quick.snapshot', {'id': context['id']})
        locks = snapshot['file_locks']
        assert locks['files'][0]['status'] == 'blocked'
        assert locks['scanned_count'] == (1 if selection == 'file' else 3)
        assert not locks['partial']
        owner = next(p for p in locks['processes'] if p['pid'] == child.pid)
        assert owner['can_release'] and owner['image']
        result = finish(app, app.call('quick.release', {'id': context['id'], 'process': owner['key'], 'confirm': True, 'mode': 'terminate'}))
        assert result['status'] == 'completed', result
        assert child.wait(timeout=3) == 1
        locks = app.call('quick.snapshot', {'id': context['id']})['file_locks']
        assert not any(p['pid'] == child.pid for p in locks['processes']) and locks['files'][0]['status'] == 'available'
        assert path.read_text() == 'preserve original'
        path.unlink()
        assert not path.exists()
    finally:
        if child.poll() is None: child.terminate(); child.wait(timeout=5)
        child.stdout.close(); child.stderr.close()


@pytest.mark.skipif(os.name != 'nt', reason='real Windows API')
def test_empty_directory_and_missing_file_distinguished(tmp_path):
    empty = file_locks.scan([str(tmp_path)])
    assert empty['files'][0]['status'] == 'available' and not empty['partial']
    result = file_locks.scan([str(tmp_path / 'gone.txt')])
    assert not result['processes'] and result['files'][0]['status'] == 'missing'


@pytest.mark.skipif(os.name != 'nt', reason='real Windows API')
@pytest.mark.parametrize('lock_type', ['directory_handle', 'current_directory'])
def test_direct_folder_occupation(app, tmp_path, lock_type):
    root = tmp_path / '空目录'; root.mkdir()
    if lock_type == 'directory_handle':
        hold = 'a=WindowsLocks();h=a.kernel.CreateFileW(sys.argv[1],0x80000000,1,None,3,0x02000000,None)'
    else:
        hold = 'os.chdir(sys.argv[1])'
    code = f'from toolbox.features.file_locks import WindowsLocks;import os,sys,time;{hold};print("ready",flush=True);time.sleep(90)'
    child = subprocess.Popen([sys._base_executable, '-c', code, str(root)], stdout=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW, text=True,
                             env={**os.environ, 'PYTHONPATH': str(Path(file_locks.__file__).parents[2])})
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(PermissionError): root.rmdir()
        context = app.call('quick.capture', {'paths': [str(root)]})
        assert finish(app, app.call('quick.run', {'id': context['id'], 'action': 'file_locks'}))['status'] == 'completed'
        result = app.call('quick.snapshot', {'id': context['id']})['file_locks']
        assert result['files'][0]['status'] == 'blocked' and not result['partial']
        owner = next(p for p in result['processes'] if p['pid'] == child.pid)
        assert owner['kind'] == 'directory' and owner['can_release']
        finished = finish(app, app.call('quick.release', {'id': context['id'], 'process': owner['key'], 'confirm': True, 'mode': 'terminate'}))
        assert finished['status'] == 'completed', finished
        assert child.wait(timeout=3) == 1
        assert app.call('quick.snapshot', {'id': context['id']})['file_locks']['files'][0]['status'] == 'available'
        root.rmdir()
    finally:
        if child.poll() is None: child.terminate(); child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.skipif(os.name != 'nt', reason='real Windows API')
def test_scan_limit_and_directory_query_failure_are_not_success(tmp_path, monkeypatch):
    for n in range(8): (tmp_path / f'{n}.txt').write_text('keep')
    monkeypatch.setattr(file_locks, 'MAX_ITEMS', 4)
    result = file_locks.scan([str(tmp_path)])
    assert result['partial'] and result['warnings'] and result['scanned_count'] == 4
    assert result['files'][0]['status'] == 'incomplete'
    def fail(*a): raise ValueError('此文件系统不支持目录查询')
    monkeypatch.setattr(file_locks.WindowsLocks, 'directory_pids', fail)
    monkeypatch.setattr(file_locks, 'MAX_ITEMS', 100)
    result = file_locks.scan([str(tmp_path)])
    assert result['partial'] and result['files'][0]['status'] == 'incomplete'
    assert any('不支持' in warning for warning in result['warnings'])


def test_junctions_not_followed_and_cancel_propagates(tmp_path, monkeypatch):
    child = tmp_path / 'link'; child.mkdir(); (child / 'outside.txt').write_text('keep')
    monkeypatch.setattr(Path, 'is_junction', lambda self: self == child)
    items, warnings = file_locks.enumerate_paths([str(tmp_path)], lambda: None)
    assert str(child) in items and str(child / 'outside.txt') not in items
    assert warnings
    def cancel(): raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        file_locks.enumerate_paths([str(tmp_path)], cancel)


@pytest.mark.skipif(os.name != 'nt', reason='Windows sessions')
def test_services_and_unknown_service_list_are_read_only(monkeypatch):
    api = file_locks.WindowsLocks()
    assert not api.directory_process(os.getpid(), set())['can_release']
    pid = 999999
    monkeypatch.setattr(api, 'process', lambda p: {'critical': False, 'start': '123', 'image': r'C:\test\app.exe'})
    def session(p, out): out._obj.value = 1; return True
    monkeypatch.setattr(api.kernel, 'ProcessIdToSessionId', session)
    assert api.directory_process(pid, set())['can_release']
    assert not api.directory_process(pid, None)['can_release']
    assert not api.directory_process(pid, {pid})['can_release']


@pytest.mark.skipif(os.name != 'nt', reason='registry API fixture')
def test_file_and_folder_menu_migration_and_disable_preference(app, tmp_path, monkeypatch):
    import contextlib
    import winreg
    from toolbox.features import relay
    registry = {}
    exe = tmp_path / 'tool.exe'; exe.write_bytes(b'test fixture')
    monkeypatch.setenv('WINTOOLBOX_APP_EXE', str(exe))
    @contextlib.contextmanager
    def create(hive, path): yield registry.setdefault(path, {})
    @contextlib.contextmanager
    def opened(hive, path):
        if path not in registry: raise FileNotFoundError(path)
        yield registry[path]
    monkeypatch.setattr(winreg, 'CreateKey', create)
    monkeypatch.setattr(winreg, 'OpenKey', opened)
    monkeypatch.setattr(winreg, 'SetValueEx', lambda key, name, *args: key.update({name: args[-1]}))
    monkeypatch.setattr(winreg, 'QueryValueEx', lambda key, name: (key[name], winreg.REG_SZ))
    monkeypatch.setattr(winreg, 'DeleteKey', lambda hive, path: registry.pop(path))
    monkeypatch.setattr(relay, 'notify_shell_associations', lambda: None)
    old = file_locks.REGISTRY_ROOTS[0]
    registry[old] = {}
    registry[old + r'\command'] = {'': f'"{exe}" --file-locks "%1"'}
    assert not app.call('quick.lock_menu', {})['enabled']
    assert app.call('quick.lock_menu', {'initialize': True})['enabled']
    assert all(root + r'\command' in registry for root in file_locks.REGISTRY_ROOTS)
    assert not app.call('quick.lock_menu', {'enabled': False})['enabled']
    assert not registry
    assert not app.call('quick.lock_menu', {'initialize': True})['enabled']
    assert not registry
