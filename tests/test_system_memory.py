from pathlib import Path
from types import SimpleNamespace
import pytest
from toolbox.features import system_memory as memory
from toolbox.features.memory_session import MemorySession
from toolbox.features.memory_worker import serve, commands, clean_memory
import socket
import threading
import json


def test_session_reuses_one_authorization_and_worker_exits_on_close():
    launches, calls, workers = [], [], []
    def launcher(port, token):
        launches.append(port)
        def worker():
            connection = socket.create_connection(('127.0.0.1', port))
            serve(connection, token, lambda mode: calls.append(mode) or {'operations': list(commands(mode))})
        thread = threading.Thread(target=worker, daemon=True); thread.start(); workers.append(thread)
    session = MemorySession(launcher)
    try:
        assert session.clean('default', lambda *a: None)['operations'] == [2, 5]
        assert session.clean('full', lambda *a: None)['operations'] == [2, 3, 4]
        assert len(launches) == 1
        assert calls == ['default', 'full']
    finally: session.close()
    workers[0].join(2)
    assert not workers[0].is_alive()


def test_worker_rejects_commands_and_paths_before_execution():
    client, worker = socket.socketpair()
    calls = []
    thread = threading.Thread(target=serve, args=(worker, 'test', lambda mode: calls.append(mode)), daemon=True)
    thread.start()
    with client, client.makefile('rwb', buffering=0) as stream:
        stream.readline()
        for payload in [{'mode': 'shell'}, {'mode': 'default', 'path': 'arbitrary.exe'}, ['full']]:
            stream.write((json.dumps(payload) + '\n').encode())
            assert json.loads(stream.readline())['ok'] is False
    thread.join(2)
    assert calls == []
    assert not thread.is_alive()


def test_cancelled_authorization_is_not_retried_automatically():
    calls = []
    def launcher(*args):
        calls.append(1)
        raise ValueError('未获得 Windows 管理员授权')
    session = MemorySession(launcher)
    with pytest.raises(ValueError, match='未获得'): session.clean('default', lambda *a: None)
    assert calls == [1]
    assert session.connection is None


def test_clean_returns_actual_delta_and_propagates_failure(monkeypatch):
    values = iter([{'available': 100}, {'available': 90}])
    monkeypatch.setattr(memory, 'snapshot', lambda: next(values))
    job = SimpleNamespace(params={'mode': 'default'}, check_cancelled=lambda: None, progress=lambda *a: None)
    session = SimpleNamespace(clean=lambda mode, progress: {'operations': [2, 5]})
    assert memory.clean(job, session)['available_change'] == -10
    monkeypatch.setattr(memory, 'snapshot', lambda: {'available': 100})
    def fail(*args): raise ValueError('区域清理失败')
    session.clean = fail
    with pytest.raises(ValueError, match='区域清理失败'): memory.clean(job, session)


def test_native_failure_is_not_reported_as_success(monkeypatch):
    from toolbox.features import memory_worker
    monkeypatch.setattr(memory_worker, 'enable_privilege', lambda: None)
    def native(*args): return -1073741727
    monkeypatch.setattr(memory_worker.ctypes, 'WinDLL', lambda *a: SimpleNamespace(NtSetSystemInformation=native))
    with pytest.raises(RuntimeError, match='NTSTATUS'): clean_memory('default')


def test_invalid_mode_never_requests_authorization():
    session = MemorySession(lambda *a: pytest.fail('Unexpected elevation'))
    with pytest.raises(ValueError): session.clean('default & anything', lambda *a: None)


def test_unrelated_local_connection_cannot_supply_cleanup_result():
    threads = []
    def launcher(port, token):
        def worker():
            with socket.create_connection(('127.0.0.1', port)) as impostor:
                impostor.sendall(b'{"token":"wrong"}\n')
                assert impostor.recv(1) == b''
            serve(socket.create_connection(('127.0.0.1', port)), token,
                  lambda mode: {'operations': list(commands(mode))})
        thread = threading.Thread(target=worker, daemon=True); thread.start(); threads.append(thread)
    session = MemorySession(launcher)
    try: assert session.clean('default', lambda *a: None)['operations'] == [2, 5]
    finally: session.close()
    threads[0].join(2)
    assert not threads[0].is_alive()


def test_modified_memreduct_is_not_executed(tmp_path, monkeypatch):
    executable = tmp_path / 'memreduct/memreduct.exe'
    executable.parent.mkdir(); executable.write_bytes(b'not upstream')
    monkeypatch.setenv('WINTOOLBOX_TOOLS', str(tmp_path))
    with pytest.raises(ValueError, match='校验失败'): memory.executable()


def test_preferences_persist_and_are_snapshotted_for_orb_and_page(tmp_path):
    jobs = SimpleNamespace(submit=lambda name, params: {'tool': name, 'params': params})
    service = memory.MemorySettings(SimpleNamespace(data_dir=tmp_path, jobs=jobs))
    assert service.get() == {'mode': 'default'}
    service.save({'mode': 'full'})
    task = service.submit({})
    assert task == {'tool': 'memory.clean', 'params': {'mode': 'full'}}
    with pytest.raises(ValueError): service.save({'mode': 'full -unexpected'})
    service.save({'mode': 'default'})
    assert task['params']['mode'] == 'full'
    assert memory.MemorySettings(service.app).get() == {'mode': 'default'}
