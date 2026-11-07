import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolbox.features import stackchan
from toolbox.features.stackchan_panel import SPEEDS, glyph, panel_packet
from toolbox.features.bambu_monitor import private_host, report, studio_printer


def test_studio_readonly_import_checksum_and_credentials(tmp_path):
    path = tmp_path / 'studio.conf'
    data = {'app': {'user_last_selected_machine': 'P2S123456'}, 'access_code': {'P2S123456': 'ABCD1234'},
            'presets': {'machine': 'Bambu Lab P2S 0.4 nozzle'}}
    original = json.dumps(data) + '\nchecksum-data'
    path.write_text(original, encoding='utf-8')
    imported = studio_printer(path)
    assert imported['printer_serial'] == 'P2S123456' and imported['printer_access_code'] == 'ABCD1234'
    assert imported['printer_name'] == 'Bambu P2S'
    assert path.read_text('utf-8') == original


def test_printer_unknown_values_and_partial_report():
    r = report({'gcode_state': 'RUNNING', 'mc_percent': '78', 'mc_remaining_time': 143, 'layer_num': 4})
    assert r['state'] == 'running' and r['progress'] == 78 and r['remaining_minutes'] == 143
    assert r['nozzle'] is None and r['total_layers'] is None
    for value in (True, None, 'bad', float('nan'), -1, 101):
        assert report({'mc_percent': value})['progress'] is None
    assert report({})['state'] == 'unknown'
    assert report({'gcode_file': '/sdcard/model.3mf'})['title'] == 'model.3mf'


def test_lan_and_vpn_addresses_are_supported_but_not_public_targets():
    assert private_host('192.168.31.10') == '192.168.31.10'
    assert private_host('100.100.20.8') == '100.100.20.8'
    for host in ('127.0.0.1', '0.0.0.0', '169.254.1.2', '8.8.8.8', 'example.com'):
        with pytest.raises(ValueError):
            private_host(host)


def test_printer_secret_and_rotation_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(stackchan, 'protect', lambda v, decrypt=False: v.removeprefix('dpapi:') if decrypt else 'dpapi:' + v)
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        value = service.save({'printer_serial': 'P2S123456', 'printer_access_code': 'ABCD1234', 'rotation_speed': 'fast'})
        assert value['settings']['has_printer_access_code']
        assert 'ABCD1234' not in json.dumps(value)
        for patch in ({'rotation_pages': []}, {'rotation_pages': ['tasks', 'tasks']},
                      {'rotation_speed': 'bad'}, {'printer_enabled': True}):
            with pytest.raises(ValueError):
                service.save(patch)
    finally:
        service.close()


def test_partial_printer_save_persists_ip_without_resetting_other_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(stackchan, 'protect', lambda v, decrypt=False: v.removeprefix('dpapi:') if decrypt else 'dpapi:' + v)
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        service.save({'printer_serial': 'P2S123456', 'printer_access_code': 'ABCD1234', 'rotation_speed': 'fast', 'city': '南京'})
        result = service.save({'printer_host': '172.26.71.98', 'printer_enabled': True})
        assert result['settings']['printer_host'] == '172.26.71.98'
        assert result['settings']['has_printer_access_code']
        assert result['settings']['rotation_speed'] == 'fast'
        assert result['settings']['city'] == '南京'
        assert 'ABCD1234' not in json.dumps(result)
        restored = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
        try:
            assert restored.config['printer_enabled']
            assert restored.config['printer_host'] == '172.26.71.98'
            assert restored.config['printer_access_protected'] == 'dpapi:ABCD1234'
        finally:
            restored.close()
    finally:
        service.close()


def test_packet_budget_with_distinct_cjk_names_and_session_paging():
    # Deliberately unique characters prevent an unrealistically tiny shared atlas.
    rows = [{'id': str(i), 'title': ''.join(chr(0x4e00 + i * 80 + j) for j in range(80)),
             'state': 'running', 'updated_at': 1792390000} for i in range(9)]
    sessions = {'rows': rows, 'available': True, 'sampled_at': 1792390000, 'total': 9, 'running': 9, 'waiting': 0}
    printer = {'title': '打印任务名称用于检查字形与载荷边界'}
    config = {**stackchan.DEFAULTS, 'rotation_speed': 'rapid'}
    source = {'v': 1, 'epoch': 1792390000, 'tz_offset': 480, 'metrics': {}, 'weather': {}, 'quota': {}}
    first = panel_packet(source, sessions, printer, config, now=0)
    assert first['sessions']['page'] == 1 and len(first['sessions']['rows']) == 4
    second = panel_packet(source, sessions, printer, config, now=5)
    assert second['sessions']['page'] == 2 and second['sessions']['rows'][0]['id'] == '4'
    held = panel_packet(source, sessions, printer, config, now=10, hold_group=1)
    assert held['sessions']['page'] == 2
    assert len(json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= 7000
    for char, (width, encoded) in first['glyphs'].items():
        assert len(base64.b64decode(encoded)) == (width * 22 + 7) // 8
        assert len(char) <= 1


def test_real_status_normalization_never_calls_idle_completed(monkeypatch):
    from toolbox.features import feishu_bridge
    from toolbox.features.stackchan_panel import read_sessions
    raw = {'threads': [{'id': 'a', 'kind': 'codex', 'hostId': 'local', 'status': 'idle', 'title': '旧任务'},
                       {'id': 'b', 'kind': 'codex', 'hostId': 'local', 'status': 'active', 'title': '当前任务'},
                       {'id': 'c', 'kind': 'codex', 'hostId': 'local', 'status': 'notLoaded', 'title': '未加载'}]}
    class Client:
        def tool(self, name, params):
            assert name == 'list_threads'
            return {'structuredContent': raw}
        def close(self):
            pass
    monkeypatch.setattr(feishu_bridge, 'connect_codex', lambda *a, **k: Client())
    value = read_sessions('this-chat')
    assert [r['state'] for r in value['rows']] == ['running', 'idle']
    assert value['running'] == 1 and value['total'] == 2


def test_waiting_status_spellings_and_objects_are_visible_across_local_sessions(monkeypatch):
    from toolbox.features import feishu_bridge
    from toolbox.features.stackchan_panel import read_sessions
    statuses = ['needsInput', 'needsAttention', 'needs-input', 'needs_attention', {'type': 'needsInput'}]
    class Client:
        def tool(self, name, params):
            assert params['limit'] == 50
            return {'structuredContent': {'threads': [
                {'id': str(i), 'kind': 'codex', 'hostId': 'local', 'status': status}
                for i, status in enumerate(statuses)]}}
        def close(self):
            pass
    monkeypatch.setattr(feishu_bridge, 'connect_codex', lambda *a, **k: Client())
    value = read_sessions('anchor')
    assert value['waiting'] == 5 and all(r['state'] == 'waiting' for r in value['rows'])
