import json
import time
from types import SimpleNamespace

import pytest

from toolbox.features import bambu_cloud, bambu_monitor, stackchan


def test_cloud_report_filters_credentials_and_malformed_fields():
    line = bambu_cloud.PREFIX + json.dumps({'event': 'report', 'payload': {
        'access_token': 'secret', 'print': {'gcode_state': 'RUNNING', 'mc_percent': 52,
        'nozzle_temper': {'token': 'secret'}, 'subtask_name': ['bad'], 'access_code': 'secret'}}})
    result = bambu_cloud.parse_event(line)
    assert result == {'event': 'report', 'data': {'gcode_state': 'RUNNING', 'mc_percent': 52}}
    for value in ('sdk noise', bambu_cloud.PREFIX + '[]', bambu_cloud.PREFIX + '{',
                  bambu_cloud.PREFIX + ' ' * 132001,
                  bambu_cloud.PREFIX + json.dumps({'event': 'report', 'payload': {'print': []}})):
        assert bambu_cloud.parse_event(value) is None


def test_cloud_import_needs_selected_printer_but_no_lan_access_code(tmp_path):
    path = tmp_path / 'studio.conf'
    original = json.dumps({'app': {'user_last_selected_machine': 'P2S123456'},
                           'presets': {'machine': 'Bambu Lab P2S 0.4 nozzle'}}) + '\nchecksum'
    path.write_text(original, encoding='utf-8')
    assert bambu_monitor.studio_printer(path, require_access=False) == {
        'printer_serial': 'P2S123456', 'printer_name': 'Bambu P2S'}
    with pytest.raises(ValueError):
        bambu_monitor.studio_printer(path)
    assert path.read_text('utf-8') == original


def test_cloud_settings_persist_without_ip_or_lan_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(stackchan.DeskService, 'start', lambda self: None)
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        result = service.save({'printer_connection': 'cloud', 'printer_enabled': True,
                               'printer_serial': 'P2S123456'})
        assert not result['settings']['printer_host']
        assert not result['settings']['has_printer_access_code']
        with pytest.raises(ValueError):
            service.save({'printer_connection': 'lan'})
        with pytest.raises(ValueError):
            service.save({'printer_connection': 'other'})
        restored = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
        try:
            assert restored.config['printer_connection'] == 'cloud'
            assert restored.config['printer_enabled']
        finally:
            restored.close()
    finally:
        service.close()


def test_import_uses_selected_cloud_mode_without_lan_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(stackchan.DeskService, 'start', lambda self: None)
    monkeypatch.setattr(stackchan, 'studio_printer', lambda *, require_access: {'printer_serial': 'P2S123456'}
                        if not require_access else pytest.fail('cloud import must not require LAN access'))
    monkeypatch.setattr(stackchan, 'discover', lambda *_: pytest.fail('cloud import must not scan the LAN'))
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        service.import_printer({'connection': 'cloud'})
        deadline = time.monotonic() + 1
        while service.status()['printer_import'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        result = service.status()
        assert result['printer_import'] == 'done'
        assert result['settings']['printer_connection'] == 'cloud'
        assert result['settings']['printer_serial'] == 'P2S123456'
    finally:
        service.close()


def test_cloud_delta_merge_disconnect_staleness_and_old_callback(tmp_path, monkeypatch):
    instances = []
    class FakeMonitor:
        def __init__(self, data_dir, callback):
            self.callback = callback
            self.closed = False
            instances.append(self)
        def start(self, serial):
            self.serial = serial
        def close(self):
            self.closed = True
    monkeypatch.setattr(bambu_cloud, 'CloudMonitor', FakeMonitor)
    now = [1000]
    monkeypatch.setattr(bambu_monitor.time, 'time', lambda: now[0])
    printer = bambu_monitor.Printer(data_dir=tmp_path)
    config = {'printer_enabled': True, 'printer_connection': 'cloud', 'printer_serial': 'P2S123456'}
    try:
        printer.configure(config, lambda _: pytest.fail('cloud must not decrypt LAN credentials'))
        monitor = instances[-1]
        monitor.callback({'event': 'server', 'code': 0})
        assert not printer.snapshot()['available']
        monitor.callback({'event': 'report', 'data': {'gcode_state': 'RUNNING', 'mc_percent': 52, 'nozzle_temper': 230}})
        monitor.callback({'event': 'report', 'data': {'mc_remaining_time': 290}})
        result = printer.snapshot()
        assert result['available'] and result['source'] == 'cloud'
        assert (result['progress'], result['nozzle'], result['remaining_minutes']) == (52, 230, 290)
        monitor.callback({'event': 'server', 'code': 5})
        assert not printer.snapshot()['available'] and not printer.snapshot()['connected']
        assert '过期' in printer.snapshot()['error']
        monitor.callback({'event': 'report', 'data': {'mc_percent': 53}})
        now[0] += 121
        assert not printer.snapshot()['available'] and '2分钟' in printer.snapshot()['error']
        printer.configure({**config, 'printer_enabled': False}, lambda _: None)
        assert monitor.closed
        monitor.callback({'event': 'report', 'data': {'mc_percent': 99}})
        assert not printer.snapshot()['available'], 'closed monitor must not overwrite a new connection'
        printer.configure(config, lambda _: None)
        now[0] += 121
        assert not printer.snapshot()['available'] and '2分钟' in printer.snapshot()['error']
    finally:
        printer.close()
    assert all(m.closed for m in instances)
