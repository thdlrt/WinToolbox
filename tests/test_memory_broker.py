from pathlib import Path
from types import SimpleNamespace
import pytest
from toolbox.features.memory_broker import PersistentMemoryCleaner, elevate

SID = 'S-1-5-21-100-200-300-1001'


class Broker:
    def __init__(self):
        self.installed = self.present = False
        self.elevations = []
        self.calls = []
        self.failure = None

    def run(self, path, action, *args):
        if action == '--identity': return {'ok': True, 'sid': SID}
        if action == '--status': return {'ok': True, 'installed': self.installed, 'present': self.present, 'protocol': 1}
        assert args[0] == SID
        self.calls.append(args[1])
        if self.failure: raise ValueError(self.failure)
        return {'ok': True, 'protocol': 1, 'operations': [2, 5] if args[1] == 'default' else [2, 3, 4]}

    def install(self, path, action, sid):
        assert sid == SID
        self.elevations.append(action)
        self.installed = self.present = action == '--install'

    def client(self): return PersistentMemoryCleaner(self.run, self.install, lambda: Path('fixture.exe'))


def test_one_install_survives_new_backend_and_worker_instances():
    broker = Broker()
    first = broker.client()
    assert first.clean('default', lambda *a: None)['operations'] == [2, 5]
    first.close()
    for _ in range(3):
        client = broker.client()
        assert client.clean('full', lambda *a: None)['operations'] == [2, 3, 4]
        client.close()
    assert broker.elevations == ['--install']
    assert broker.calls == ['default', 'full', 'full', 'full']


def test_operation_failure_never_reinstalls_or_retries():
    broker = Broker(); broker.installed = True; broker.present = True
    broker.failure = 'NTSTATUS failure'
    with pytest.raises(ValueError, match='NTSTATUS'): broker.client().clean('default', lambda *a: None)
    assert not broker.elevations and broker.calls == ['default']
    broker.failure = None
    broker.client().clean('default', lambda *a: None)
    assert not broker.elevations


def test_broken_install_requires_explicit_repair_not_another_automatic_prompt():
    broker = Broker(); broker.present = True
    with pytest.raises(ValueError, match='修复'): broker.client().clean('default', lambda *a: None)
    assert not broker.elevations and not broker.calls
    broker.client().configure(SimpleNamespace(check_cancelled=lambda: None, progress=lambda *a: None))
    broker.client().clean('default', lambda *a: None)
    assert broker.elevations == ['--install']


def test_uac_cancellation_does_not_clean_or_retry():
    broker = Broker()
    def refuse(*args): broker.elevations.append('refused'); raise ValueError('已取消授权')
    client = PersistentMemoryCleaner(broker.run, refuse, lambda: Path('fixture.exe'))
    with pytest.raises(ValueError, match='已取消'): client.clean('default', lambda *a: None)
    assert broker.elevations == ['refused'] and not broker.calls


def test_invalid_mode_or_sid_cannot_trigger_elevation():
    broker = Broker()
    with pytest.raises(ValueError): broker.client().clean('default & shell', lambda *a: None)
    assert not broker.elevations and not broker.calls
    for action, sid in [('--serve', SID), ('--install', SID + ' & anything'), ('--install', 'S-1-1-0')]:
        with pytest.raises(ValueError): elevate(Path('fixture.exe'), action, sid)


def test_read_only_status_and_explicit_removal():
    broker = Broker(); client = broker.client()
    assert not client.status()['installed'] and not broker.elevations
    job = SimpleNamespace(check_cancelled=lambda: None, progress=lambda *a: None)
    assert client.configure(job)['installed']
    assert not client.configure(job, remove=True)['installed']
    assert broker.elevations == ['--install', '--uninstall']

@pytest.mark.parametrize('source,expected', [
    (r'\\?\C:\工具箱\tools\cleaner.exe', r'C:\工具箱\tools\cleaner.exe'),
    (r'\\?\UNC\server\share\cleaner.exe', r'\\server\share\cleaner.exe'),
    (r'C:\plain\cleaner.exe', r'C:\plain\cleaner.exe'),
])
def test_framework_launch_normalizes_extended_paths(source, expected, monkeypatch):
    from toolbox.features import memory_broker as module
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.invoke(source, '--identity')['ok']
    assert calls == [[expected, '--identity']]


def test_framework_crash_reports_exit_and_logs_diagnostics(monkeypatch, caplog):
    from toolbox.features import memory_broker as module
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=-532462766, stdout='', stderr='System.ArgumentException: invalid configuration path'))
    with pytest.raises(ValueError, match='退出码 -532462766'):
        module.invoke('cleaner.exe', '--identity')
    assert 'System.ArgumentException' in caplog.text
    assert '--identity' in caplog.text


def test_automatic_cleanup_never_installs_even_if_component_disappeared():
    broker = Broker()
    with pytest.raises(ValueError, match='自动清理已跳过'):
        broker.client().clean('default', lambda *a: None, allow_install=False)
    assert not broker.elevations and not broker.calls
    broker.installed = broker.present = True
    assert broker.client().clean('default', lambda *a: None, allow_install=False)['operations'] == [2, 5]


def test_memory_scheduler_lifecycle_excludes_fixture_apps_and_stops_before_exit(tmp_path):
    from toolbox.app import App
    app = App(tmp_path, register_live=False)
    try:
        assert app.memory_automation.thread is None
        app.memory_cleaner_start()
        assert app.memory_automation.thread.is_alive()
        app.prepare_exit()
        assert app.memory_automation.stop.is_set()
        assert not app.memory_automation.thread.is_alive()
    finally:
        app.close()
