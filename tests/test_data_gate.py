"""RPC edits and snapshot commits must share one synchronization boundary."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from toolbox.app import App


def lightweight_app():
    app = App.__new__(App)
    app.handlers = {}
    app.data_lock = threading.RLock()
    app.maintenance = False
    return app


def test_snapshot_waits_for_inflight_edit_and_observes_complete_record():
    app = lightweight_app()
    entered, finish_edit, attempted = (threading.Event() for _ in range(3))
    record = {}

    def edit(_):
        record['text'] = 'new paragraph'
        entered.set()
        assert finish_edit.wait(5)
        record['audio'] = 'new.wav'

    def snapshot():
        attempted.set()
        with app.data_lock:
            return dict(record)

    app.register('practice.save', edit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(app.call, 'practice.save')
        assert entered.wait(5)
        copying = pool.submit(snapshot)
        try:
            assert attempted.wait(5)
            assert not copying.done()
        finally:
            finish_edit.set()
        writing.result(timeout=5)
        assert copying.result(timeout=5) == {'text': 'new paragraph', 'audio': 'new.wav'}


def test_maintenance_rejects_edits_but_status_and_cancel_do_not_wait_for_gate():
    app = lightweight_app()
    edits = []
    app.register('settings.update', lambda p: edits.append(p))
    allowed = ('app.info', 'jobs.get', 'jobs.list', 'jobs.cancel')
    for name in allowed:
        app.register(name, lambda p: p)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with app.data_lock:
            app.maintenance = True
            for name in allowed:
                assert pool.submit(app.call, name, {'id': 'fixture'}).result(timeout=2) == {'id': 'fixture'}
            with pytest.raises(RuntimeError, match='正在同步或恢复'):
                pool.submit(app.call, 'settings.update', {'theme': 'dark'}).result(timeout=2)
    assert edits == []


def test_request_already_waiting_for_gate_rechecks_maintenance():
    app = lightweight_app()
    attempted = threading.Event()
    underlying = threading.RLock()

    class ObservedGate:
        def __enter__(self):
            attempted.set()
            underlying.acquire()

        def __exit__(self, *args):
            underlying.release()

    app.data_lock = ObservedGate()
    edits = []
    app.register('practice.delete', lambda p: edits.append(p))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with underlying:
            waiting = pool.submit(app.call, 'practice.delete', {'id': 'fixture'})
            assert attempted.wait(5)
            app.maintenance = True
        with pytest.raises(RuntimeError, match='正在同步或恢复'):
            waiting.result(timeout=5)
    assert edits == []
