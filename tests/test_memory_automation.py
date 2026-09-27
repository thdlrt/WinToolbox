"""Deterministic timer, threshold and no-elevation automatic-clean tests."""
import threading
from types import SimpleNamespace

import pytest

from toolbox.features import system_memory as memory
from toolbox.config_backup import validate_config


class Jobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = {}
        self.records = {}
    def submit(self, tool, params):
        key = str(len(self.records) + 1)
        record = {'id': key, 'tool': tool, 'params': dict(params), 'status': 'queued'}
        self.records[key] = record
        self.active[key] = SimpleNamespace(record=record)
        return record
    def get(self, key):
        return self.records[key]
    def finish(self, key, status='completed'):
        self.records[key]['status'] = status
        self.active.pop(key, None)


@pytest.fixture
def setup(tmp_path):
    now, percent, installed = [0.], [50], [True]
    jobs = Jobs()
    app = SimpleNamespace(data_dir=tmp_path, jobs=jobs, maintenance=False, data_lock=threading.RLock())
    settings = memory.MemorySettings(app)
    session = SimpleNamespace(status=lambda: {'installed': installed[0]})
    automation = memory.MemoryAutomation(app, settings, session, clock=lambda: now[0], wall=lambda: 1000 + now[0], reader=lambda: {'percent': percent[0]})
    yield app, settings, automation, now, percent, installed
    automation.close()


def test_timer_waits_selected_interval_then_obeys_cooldown(setup):
    app, settings, auto, now, _, _ = setup
    settings.save({'auto_enabled': True, 'threshold_enabled': False, 'interval_enabled': True,
                   'interval_minutes': 2, 'cooldown_minutes': 5, 'auto_mode': 'full'})
    assert auto.tick() is None
    assert settings.get()['automation']['next_interval_at'] == 1120
    now[0] = 119
    assert auto.tick() is None
    now[0] = 120
    first = auto.tick()
    assert first['params'] == {'mode': 'full', 'automatic': True, 'trigger': 'interval'}
    now[0] = 121
    assert auto.tick() is None  # Queued/running work never duplicates.
    app.jobs.finish(first['id'])
    auto.tick()  # Reconcile terminal work and start cooldown at completion.
    now[0] = 420
    assert auto.tick() is None
    now[0] = 421
    assert auto.tick()['params']['trigger'] == 'interval'


def test_threshold_requires_recovery_before_retrigger_and_respects_cooldown(setup):
    app, settings, auto, now, percent, _ = setup
    settings.save({'auto_enabled': True, 'cooldown_minutes': 1})
    percent[0] = 81
    first = auto.tick()
    assert first['params']['trigger'] == 'threshold'
    app.jobs.finish(first['id'])
    auto.tick()
    now[0] = 90
    assert auto.tick() is None  # Still high; no repeated threshold clean.
    percent[0] = 76
    assert auto.tick() is None
    percent[0] = 75
    assert auto.tick() is None  # Five percentage points of hysteresis.
    percent[0] = 82
    assert auto.tick()['params']['trigger'] == 'threshold'


def test_uninstalled_helper_never_enqueues_or_installs_then_resumes(setup):
    app, settings, auto, _, percent, installed = setup
    settings.save({'auto_enabled': True})
    percent[0], installed[0] = 95, False
    assert auto.tick() is None
    assert not app.jobs.records
    assert '不会请求管理员授权' in auto.status()['paused_reason']
    installed[0] = True
    assert auto.tick()['params']['automatic'] is True


def test_auto_clean_forbids_install_even_if_helper_disappears(monkeypatch):
    monkeypatch.setattr(memory, 'snapshot', lambda: {'available': 100})
    received = []
    def fake_clean(mode, progress, *, allow_install=True):
        received.append(allow_install)
        raise ValueError('组件已被移除')
    job = SimpleNamespace(params={'mode': 'default', 'automatic': True}, check_cancelled=lambda: None, progress=lambda *args: None)
    with pytest.raises(ValueError, match='移除'):
        memory.clean(job, SimpleNamespace(clean=fake_clean))
    assert received == [False]


def test_manual_work_and_maintenance_block_auto_queue(setup):
    app, settings, auto, _, percent, _ = setup
    settings.save({'auto_enabled': True})
    percent[0] = 90
    manual = settings.submit({})
    assert auto.tick() is None
    app.jobs.finish(manual['id'])
    app.maintenance = True
    assert auto.tick() is None
    assert '维护' in auto.status()['paused_reason']
    app.maintenance = False
    assert auto.tick()


def test_setting_change_resets_timer_and_disabling_stops_future_jobs(setup):
    _, settings, auto, now, _, _ = setup
    settings.save({'auto_enabled': True, 'threshold_enabled': False, 'interval_enabled': True, 'interval_minutes': 1})
    auto.tick()
    now[0] = 50
    settings.save({'interval_minutes': 2})
    auto.tick()
    assert auto.status()['next_interval_at'] == 1170
    now[0] = 120
    assert auto.tick() is None
    settings.save({'auto_enabled': False})
    now[0] = 200
    assert auto.tick() is None and auto.status()['next_interval_at'] is None
    assert memory.MemorySettings(settings.app).values()['interval_minutes'] == 2


@pytest.mark.parametrize('patch', [{'threshold_percent': True}, {'threshold_percent': 49}, {'threshold_percent': 100},
                                  {'interval_minutes': 0}, {'interval_minutes': 1.5}, {'cooldown_minutes': -1},
                                  {'auto_enabled': 'true'}, {'auto_mode': 'external'},
                                  {'auto_enabled': True, 'threshold_enabled': False, 'interval_enabled': False}])
def test_invalid_automation_settings_rejected_by_save_and_config_restore(setup, patch):
    _, settings, _, _, _, _ = setup
    before = settings.values()
    with pytest.raises(ValueError):
        settings.save(patch)
    with pytest.raises(ValueError):
        validate_config('memory-cleaner.json', {**before, **patch})
    assert settings.values() == before


def test_legacy_mode_only_settings_load_with_disabled_automation(tmp_path):
    from toolbox.settings import atomic_json
    atomic_json(tmp_path / 'memory-cleaner.json', {'mode': 'full'})
    settings = memory.MemorySettings(SimpleNamespace(data_dir=tmp_path))
    assert settings.values()['mode'] == 'full'
    assert settings.values()['auto_enabled'] is False
    validate_config('memory-cleaner.json', {'mode': 'full'})


def test_restart_keeps_configuration_and_starts_a_fresh_interval(setup):
    app, settings, auto, now, _, _ = setup
    settings.save({'auto_enabled': True, 'threshold_enabled': False, 'interval_enabled': True, 'interval_minutes': 3})
    auto.tick()
    now[0] = 100
    auto.close()
    restored = memory.MemorySettings(app)
    reopened = memory.MemoryAutomation(app, restored, SimpleNamespace(status=lambda: {'installed': True}),
                                       clock=lambda: now[0], wall=lambda: 1000 + now[0], reader=lambda: {'percent': 50})
    try:
        assert reopened.tick() is None
        assert reopened.status()['next_interval_at'] == 1280
        now[0] = 279
        assert reopened.tick() is None
        now[0] = 280
        assert reopened.tick()['params']['trigger'] == 'interval'
    finally:
        reopened.close()


@pytest.mark.parametrize('action', ['disable', 'close'])
def test_disable_or_close_while_helper_status_in_flight_prevents_submission(setup, action):
    app, settings, auto, _, percent, _ = setup
    settings.save({'auto_enabled': True})
    percent[0] = 99
    entered, resume = threading.Event(), threading.Event()
    def slow_status():
        entered.set()
        assert resume.wait(2)
        return {'installed': True}
    auto.session.status = slow_status
    thread = threading.Thread(target=auto.tick)
    thread.start()
    assert entered.wait(2)
    if action == 'disable':
        settings.save({'auto_enabled': False})
    else:
        auto.close()
    resume.set()
    thread.join(2)
    assert not thread.is_alive() and not app.jobs.records


def test_cancelled_queued_job_clears_running_even_after_disabling(setup):
    app, settings, auto, _, percent, _ = setup
    settings.save({'auto_enabled': True})
    percent[0] = 99
    pending = auto.tick()
    settings.save({'auto_enabled': False})
    app.jobs.finish(pending['id'], 'cancelled')
    auto.tick()
    assert not auto.status()['running']
    assert auto.status()['last_error'] == '自动清理已取消'
