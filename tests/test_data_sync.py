import json
import threading
import time
from types import SimpleNamespace

import pytest

from toolbox.data_sync import DataSync


def manager(tmp_path, **kwargs):
    app = SimpleNamespace(data_dir=tmp_path, data_lock=threading.RLock(), maintenance=False)
    return DataSync(app, **kwargs)


def test_unconfigured_start_never_reports_success_or_creates_jobs(tmp_path):
    sync = manager(tmp_path)
    sync.register('one', 'One', lambda: False, lambda _: pytest.fail('unconfigured run'))
    sync.start()
    sync.close()
    assert sync.status()['configured'] is False
    assert sync.status()['last_sync'] is None
    assert not (tmp_path / 'jobs').exists()


def test_retry_partial_failure_persistence_and_success_all_services(tmp_path):
    now = [0]
    sync = manager(tmp_path, clock=lambda: now[0])
    calls = []
    offline = [True]
    def ledger(_):
        calls.append('ledger')
        if offline[0]:
            raise OSError('fixture offline')
    sync.register('ledger', '记账', lambda: True, ledger)
    sync.register('memory', '记忆', lambda: True, lambda _: calls.append('memory'))
    sync.tick()
    assert calls == ['ledger', 'memory']
    assert sync.status()['last_sync'] is None
    assert 'offline' in sync.status()['error']
    restored = manager(tmp_path)
    restored.register('ledger', '记账', lambda: True, ledger)
    assert 'offline' in restored.status()['services'][0]['error']
    now[0] = 4
    sync.tick()
    assert len(calls) == 2
    now[0] = 5
    offline[0] = False
    sync.tick()
    assert calls == ['ledger', 'memory', 'ledger']
    assert sync.status()['last_sync'] and sync.status()['error'] is None
    assert json.loads(sync.path.read_text())['last_sync']


def test_changes_debounce_and_concurrent_manual_request_coalesce(tmp_path):
    now = [0]
    sync = manager(tmp_path, clock=lambda: now[0])
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def run(_):
        calls.append('run')
        if len(calls) == 1:
            entered.set()
            assert finish.wait(3)
    sync.register('one', 'One', lambda: True, run)
    sync.request('one', immediate=False)
    now[0] = .5
    sync.request('one', immediate=False)
    now[0] = 1
    sync.tick()
    assert not calls
    now[0] = 2
    thread = threading.Thread(target=sync.tick)
    thread.start()
    assert entered.wait(3)
    for _ in range(5):
        assert sync.run('one', SimpleNamespace(params={}))['queued']
    assert sync.status()['syncing']
    with pytest.raises(RuntimeError, match='等待'):
        sync.assert_idle()
    finish.set()
    thread.join(3)
    assert sync.status()['last_sync'] is None  # outstanding request is not success
    sync.tick()
    assert calls == ['run', 'run']
    assert sync.status()['last_sync']
    sync.tick()
    assert len(calls) == 2


def test_periodic_config_change_and_maintenance(tmp_path):
    now, configured, calls = [0], [False], []
    sync = manager(tmp_path, clock=lambda: now[0])
    sync.register('one', 'One', lambda: configured[0], lambda _: calls.append(now[0]))
    sync.tick()
    configured[0] = True
    sync.request()
    sync.app.maintenance = True
    sync.tick()
    assert not calls
    sync.app.maintenance = False
    sync.tick()
    now[0] = 59
    sync.tick()
    assert calls == [0]
    now[0] = 60
    sync.tick()
    assert calls == [0, 60]


def test_partial_migration_and_ssh_warnings_never_advance_success(tmp_path):
    sync = manager(tmp_path)
    sync.register('one', 'One', lambda: True, lambda _: {'warnings': [{'message': 'SSH offline'}]})
    sync.tick()
    assert 'SSH offline' in sync.status()['error']
    assert sync.status()['last_sync'] is None
    sync.services['one']['run'] = lambda _: {'migration_pending': 1}
    sync.request()
    sync.tick()
    assert sync.status()['last_sync'] is None
    assert sync.status()['services'][0]['last_sync'] is None


def test_memory_local_write_wakes_but_remote_ingest_does_not(tmp_path):
    from toolbox.project_memory.store import MemoryStore
    source = MemoryStore(tmp_path / 'source', device_id='source')
    target = MemoryStore(tmp_path / 'target', device_id='target')
    calls = []
    try:
        source.on_change = lambda: calls.append('source')
        target.on_change = lambda: calls.append('target')
        project = source.create_project('fixture')
        target.join_library(source.library_id)
        target.ingest_operations(source.export_operations())
        assert calls == ['source']
        source.save_entry({'title': 'local change', 'body': 'fixture', 'kind': 'knowledge', 'project_id': project['id']})
        assert calls == ['source', 'source']
    finally:
        source.close()
        target.close()


def test_startup_ignores_old_memory_auto_switch_and_has_no_auto_jobs(tmp_path, monkeypatch):
    from toolbox.app import App
    from toolbox.settings import atomic_json
    from toolbox.features import project_memory
    from toolbox.project_memory.store import machine_device_id
    atomic_json(tmp_path / 'project-memory-local' / machine_device_id() / 'sync.json', {'auto_sync': False})
    atomic_json(tmp_path / 'webdav.json', {'url': 'https://fixture.invalid', 'username': 'fixture', 'password_dpapi': 'fake'})
    calls = []
    class Ledger:
        def __init__(self, _): pass
        def close(self): pass
        def sync(self, store, job):
            calls.append('ledger')
            return {'synced_operations': len(store.export_operations())}
    class Memory:
        def __init__(self, _): pass
        def close(self): pass
        def sync(self, store, job, verify=False):
            calls.append('memory')
            return {'uploaded': 0, 'downloaded': 0}
    monkeypatch.setattr('toolbox.ledger_service.LedgerRemote', Ledger)
    monkeypatch.setattr(project_memory, 'MemoryRemote', Memory)
    app = App(tmp_path, register_live=False)
    try:
        deadline = time.monotonic() + 4
        while not app.data_sync.status()['last_sync'] and time.monotonic() < deadline:
            time.sleep(.01)
        assert set(calls) == {'ledger', 'memory'}
        assert app.call('data_sync.status')['last_sync']
        assert not any(row['tool'] in ('expenses.sync', 'memory.sync.run') for row in app.jobs.list())
        assert app.call('memory.sync.status')['auto_sync']
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)


def test_prepare_exit_releases_data_gate_before_waiting_for_sync(tmp_path):
    from toolbox.app import App
    app = App(tmp_path, register_features=False, register_live=False)
    entered = threading.Event()
    def sync_job(_):
        entered.set()
        assert app.data_sync.stop.wait(3)
        with app.data_lock:
            pass  # ledger materialization takes this gate at the end of sync
    app.data_sync.register('fixture', 'Fixture', lambda: True, sync_job)
    app.data_sync.request()
    try:
        assert entered.wait(3)
        thread = threading.Thread(target=lambda: app.call('app.prepare_exit'))
        thread.start()
        thread.join(3)
        assert not thread.is_alive()
        assert not app.data_sync.worker.is_alive()
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)


def test_file_transfer_status_never_starts_copy_jobs(tmp_path):
    sync = manager(tmp_path)
    sync.app.relay = SimpleNamespace(config=lambda: {'shared_configured': True, 'migration': {}})
    sync.app.filesync = SimpleNamespace(rules=lambda: [{'last': {'ok': True, 'time': 10}}])
    sync.app.jobs = SimpleNamespace(list=lambda: [{'tool': 'relay.upload', 'status': 'completed',
                                                 'finished_at': '2026-09-27T10:00:00+00:00'}])
    state = sync.now()
    assert state['last_sync'] is None and not state['configured']
    assert [row['mode'] for row in state['transfers']] == ['rules', 'manual']
    assert all(row['last_sync'] for row in state['transfers'])


def test_shutdown_cancels_and_waits_for_manual_service_job(tmp_path):
    sync = manager(tmp_path)
    entered, finished = threading.Event(), threading.Event()
    job = SimpleNamespace(params={}, cancel_event=threading.Event())
    def run(context):
        entered.set()
        assert context.cancel_event.wait(3)
        finished.set()
    sync.register('one', 'One', lambda: True, run)
    thread = threading.Thread(target=lambda: sync.run('one', job))
    thread.start()
    assert entered.wait(3)
    sync.close()
    thread.join(3)
    assert finished.is_set() and not thread.is_alive()
    assert sync.run('one', job)['skipped']


def test_empty_cancel_exception_cannot_be_reported_as_success(tmp_path):
    from toolbox.jobs import Cancelled
    sync = manager(tmp_path)
    def cancelled(_):
        raise Cancelled()
    sync.register('one', 'One', lambda: True, cancelled)
    sync.tick()
    assert sync.status()['last_sync'] is None
    assert sync.status()['services'][0]['error']


@pytest.mark.parametrize('old_fails', [False, True])
def test_connection_change_discards_inflight_old_connection_results(tmp_path, old_fails):
    sync = manager(tmp_path)
    entered, finish = threading.Event(), threading.Event()
    calls, results = [], []
    def run(_):
        calls.append(len(calls))
        if len(calls) == 2:
            entered.set()
            assert finish.wait(3)
            if old_fails:
                raise OSError('old connection offline')
        return {'uploaded': 1}
    sync.register('one', 'One', lambda: True, run)
    sync.tick()
    assert sync.status()['last_sync']
    sync.request()
    def old_run():
        try:
            results.append(sync.run('one'))
        except OSError:
            pass
    thread = threading.Thread(target=old_run)
    thread.start()
    assert entered.wait(3)
    sync.connection_changed()
    state = sync.status()
    assert state['syncing'] and state['last_sync'] is None and state['error'] is None
    assert state['services'][0]['last_sync'] is None
    persisted = json.loads(sync.path.read_text())
    assert persisted['last_sync'] is None and persisted['services']['one']['last_sync'] is None
    finish.set()
    thread.join(3)
    assert not thread.is_alive()
    assert sync.status()['last_sync'] is None and sync.status()['error'] is None
    if not old_fails:
        assert results == [{'queued': True, 'reason': 'connection_changed'}]
    sync.tick()
    assert len(calls) == 3 and sync.status()['last_sync']
