"""Temporary journals and fake power/registry; never edit real GPU preferences."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolbox.features.gpu_guard import Guard, prefer_integrated
from toolbox.features.gpu_native import battery_metrics, merge_counters


class Registry:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.fail_path = None

    def read(self, path):
        return self.values.get(path)

    def write(self, path, value):
        if path == self.fail_path and value is not None:
            raise OSError('registry unavailable')
        self.calls.append((path, value))
        if value is None:
            self.values.pop(path, None)
        else:
            self.values[path] = value


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    registry = Registry()
    power = {'source': 'ac', 'has_battery': True, 'percent': 80}
    native = SimpleNamespace(power_status=lambda: power.copy(), adapters=lambda: [{'vendor': 0x10de}])
    guard = Guard(tmp_path / 'state', preferences=registry, native=native, exclusive=False)
    app = tmp_path / 'editor.exe'; app.touch()
    guard.edit({'path': str(app)})
    yield guard, registry, power, str(app)
    guard.close()


def test_preference_preserves_unrelated_flags():
    assert prefer_integrated('SwapEffectUpgradeEnable=1;GpuPreference=2;') == 'SwapEffectUpgradeEnable=1;GpuPreference=1;'
    assert prefer_integrated(None) == 'GpuPreference=1;'
    assert prefer_integrated('GpuPreference=1;') == 'GpuPreference=1;'


def test_battery_transition_restore_and_unknown_source(setup):
    guard, registry, power, app = setup
    registry.values[app] = 'GpuPreference=2;Other=1;'
    guard.set_enabled(True)
    assert not registry.calls
    power['source'] = 'battery'; guard.tick()
    assert registry.values[app] == 'Other=1;GpuPreference=1;'
    guard.tick()
    assert len(registry.calls) == 1
    power['source'] = 'unknown'; guard.tick()
    assert registry.values[app] == 'GpuPreference=2;Other=1;'
    assert guard.enabled and not guard.active
    power['source'] = 'battery'; guard.tick()
    guard.set_enabled(False)
    assert registry.values[app] == 'GpuPreference=2;Other=1;'


def test_restore_absent_and_preserve_external_edit(setup):
    guard, registry, power, app = setup
    power['source'] = 'battery'; guard.set_enabled(True)
    assert app in registry.values
    guard.set_enabled(False)
    assert app not in registry.values
    guard.set_enabled(True)
    registry.values[app] = 'GpuPreference=2;Other=external;'
    guard.set_enabled(False)
    assert registry.values[app] == 'GpuPreference=2;Other=external;'
    assert '保留' in guard.notice


def test_durable_journal_written_before_registry_and_crash_recovery(setup):
    guard, registry, power, app = setup
    write = registry.write
    def verify(path, value):
        journal = json.loads((guard.root / 'recovery.json').read_text('utf-8'))
        assert path in journal['entries']
        write(path, value)
    registry.write = verify
    power['source'] = 'battery'; guard.set_enabled(True)
    # A new process reads the exact prior journal, no user/system keys involved.
    other = Guard(guard.root, preferences=registry, native=guard.native, exclusive=False)
    assert app not in registry.values
    assert not other.journal
    other.close()
    registry.write = write


def test_partial_apply_failure_rolls_back(setup, tmp_path):
    guard, registry, power, app = setup
    second = tmp_path / 'second.exe'; second.touch()
    guard.edit({'path': str(second)})
    registry.fail_path = str(second)
    power['source'] = 'battery'
    with pytest.raises(OSError, match='unavailable'):
        guard.set_enabled(True)
    assert not registry.values and not guard.journal and not guard.enabled


def test_failed_restore_keeps_recovery_for_retry(setup):
    guard, registry, power, app = setup
    registry.values[app] = 'GpuPreference=2;'
    power['source'] = 'battery'; guard.set_enabled(True)
    registry.fail_path = app
    with pytest.raises(RuntimeError, match='未恢复'):
        guard.set_enabled(False)
    assert guard.journal and not guard.enabled
    registry.fail_path = None
    guard.set_enabled(False)
    assert registry.values[app] == 'GpuPreference=2;' and not guard.journal


def test_desktop_never_enables_or_writes(setup):
    guard, registry, power, app = setup
    power['has_battery'] = False
    with pytest.raises(RuntimeError, match='电池'):
        guard.set_enabled(True)
    assert not guard.enabled and not registry.calls


def test_list_edits_require_disabled_guard_and_existing_exe(setup, tmp_path):
    guard, registry, power, app = setup
    with pytest.raises(ValueError, match='exe'):
        guard.edit({'path': str(tmp_path / 'missing.exe')})
    guard.set_enabled(True)
    with pytest.raises(RuntimeError, match='停用'):
        guard.edit({'path': app, 'remove': True})
    guard.set_enabled(False)
    guard.edit({'path': app, 'remove': True})
    assert not guard.apps


def test_rendering_option_only_writes_local_flag(setup):
    guard, registry, power, app = setup
    assert guard.rendering(True)['software_rendering']
    assert not guard.rendering(False)['software_rendering']
    assert not registry.calls


def test_scan_does_not_change_preferences_and_keeps_power_unknown(setup):
    guard, registry, power, app = setup
    guard.native.snapshot = lambda: {'power': power, 'processes': [], 'power_state': 'unknown'}
    assert guard.scan()['sample']['power_state'] == 'unknown'
    assert not registry.calls


def test_sample_failure_visible_and_rate_limited(setup):
    guard, registry, power, app = setup
    def fail():
        raise RuntimeError('counter unavailable')
    guard.native.snapshot = fail
    with pytest.raises(RuntimeError):
        guard.scan()
    assert guard.last_scan > 0 and guard.error == 'counter unavailable'


def test_counter_luid_filter_and_parallel_engines():
    nvidia = '0x00000000_0x00000001'; amd = '0x00000000_0x00000002'
    gpus = [{'vendor': 0x10de, 'name': 'RTX 5070', 'luid': nvidia}, {'vendor': 0x1002, 'name': 'AMD', 'luid': amd}]
    counters = {'usage': [(f'pid_42_luid_{nvidia}_eng_0', 20), (f'pid_42_luid_{nvidia}_eng_1', 30), (f'pid_99_luid_{amd}_eng_0', 70)],
                'memory': [(f'pid_42_luid_{nvidia}_phys_0', 1048576), (f'pid_43_luid_{nvidia}_phys_0', 2097152)]}
    rows = merge_counters(gpus, counters, resolve=lambda pid: f'C:/app{pid}.exe')
    assert len(rows) == 2 and rows[0]['pid'] == 42 and rows[0]['usage'] == 30
    assert rows[1]['usage'] == 0 and rows[1]['memory_mb'] == 2


@pytest.mark.parametrize('rate,discharging,watts', [(0xffffd120, True, 12.0), (12000, True, 12.0), (0xffffffff, True, None), (12000, False, None), (0, True, None)])
def test_battery_rate_signed_and_missing(rate, discharging, watts):
    result = battery_metrics(rate, 42000, 0xffffffff, discharging)
    assert result['discharge_w'] == watts and result['remaining_seconds'] is None
    assert result['remaining_wh'] == 42


def test_valid_runtime_only_when_discharging():
    assert battery_metrics(12000, 42000, 12600, True)['remaining_seconds'] == 12600
    assert battery_metrics(12000, 42000, 12600, False)['remaining_seconds'] is None
