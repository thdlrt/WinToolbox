import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from toolbox.features import stackchan


def test_quota_real_windows_unknown_and_multibucket():
    value = stackchan.normalize_quota({'rateLimits': {'primary': {'usedPercent': 99}}, 'rateLimitsByLimitId': {
        'codex': {'primary': {'usedPercent': 13, 'windowDurationMins': 10080, 'resetsAt': 123}, 'secondary': None},
        'extra': {'primary': {'usedPercent': None}, 'secondary': {'usedPercent': 120, 'windowDurationMins': 60}}}})
    assert value['windows'] == [
        {'bucket': 'codex', 'window': 'primary', 'minutes': 10080, 'remaining': 87, 'resets_at': 123},
        {'bucket': 'extra', 'window': 'secondary', 'minutes': 60, 'remaining': 0, 'resets_at': None}]
    assert not stackchan.normalize_quota({})['available']
    assert not stackchan.normalize_quota({'rateLimits': {'primary': {'usedPercent': float('nan')}}})['available']


@pytest.mark.parametrize('host', ['127.0.0.1', '0.0.0.0', '169.254.1.1', '8.8.8.8', 'example.com', 'http://192.168.1.1'])
def test_host_rejects_non_device_targets(host):
    with pytest.raises(ValueError):
        stackchan.safe_host(host)


def test_settings_secret_not_echoed_and_disabled_is_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(stackchan, 'protect', lambda value, decrypt=False: value.removeprefix('protected:') if decrypt else 'protected:' + value)
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        result = service.save({'host': '192.168.100.156', 'token': 'a' * 24})
        assert result['settings']['has_token']
        assert 'a' * 24 not in json.dumps(result)
        assert 'token_protected' not in result['settings']
        assert result['settings']['enabled'] is False
        assert service.save({'motion_seconds': 2})['settings']['motion_seconds'] == 2
        with pytest.raises(ValueError):
            service.save({'motion_seconds': 1})
        with pytest.raises(ValueError):
            service.save({'latitude': 32})
        with pytest.raises(ValueError):
            service.save({'default_page': 'unknown'})
    finally:
        service.close()


def test_slow_source_does_not_block_telemetry_and_old_results_dropped(tmp_path):
    entered, release = threading.Event(), threading.Event()
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    def slow():
        entered.set()
        release.wait(2)
        return {'available': True, 'value': 'old city'}
    service._slow_sample('weather', slow, 0)
    assert entered.wait(1)
    service._sample('metrics', lambda: {'cpu': {'percent': 42}})
    assert service.status()['metrics']['cpu']['percent'] == 42
    with service.lock:
        service.revision += 1
    release.set()
    deadline = time.monotonic() + 1
    while service.slow_busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert service.status()['weather'] == {}
    service.close()


def test_transient_failure_keeps_original_timestamp(tmp_path):
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    service.data['weather'] = {'available': True, 'sampled_at': 100, 'temperature': 22}
    def fail():
        raise RuntimeError('offline')
    service._sample('weather', fail)
    result = service.status()['weather']
    assert result['sampled_at'] == 100 and result['temperature'] == 22 and result['error'] == 'offline'
    service.close()


def test_background_failure_retries_after_one_minute_and_success_clears_retry(tmp_path):
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    service.data['weather'] = {'available': True, 'sampled_at': 100, 'temperature': 22}
    def fail():
        raise RuntimeError('TLS timeout')
    def wait_sample():
        deadline = time.monotonic() + 1
        while service.slow_busy and time.monotonic() < deadline:
            time.sleep(.01)
        assert not service.slow_busy
    before = time.monotonic()
    service._slow_sample('weather', fail, 0)
    wait_sample()
    assert before + 60 <= service.retry_at['weather'] <= time.monotonic() + 60
    assert service.status()['weather']['sampled_at'] == 100
    service._slow_sample('weather', lambda: {'available': True, 'sampled_at': 200}, 0)
    wait_sample()
    assert 'weather' not in service.retry_at
    assert service.status()['weather']['sampled_at'] == 200
    service.close()


def test_pending_reaction_and_motion_do_not_receive_heavy_panel_updates(tmp_path):
    calls, device = [], {}
    def send(config, packet):
        calls.append(packet)
        return {'v': 1, 'page': 'tasks', 'interaction': device.copy()}
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path), sender=send)
    try:
        packet = {'v': 1, 'glyphs': {'atlas': 'heavy'}}
        for device_state in ({'pending': {'id': 'problem'}}, {'busy': True}):
            device.clear(); device.update(device_state); calls.clear()
            service._send_panel({}, packet)
            assert calls == [{'action': 'state'}], 'urgent reaction must not wait for another font upload/decode'
        device.clear(); calls.clear()
        service.interactions.current_action = 'welcome'
        device['pending'] = {'kind': 'welcome'}
        service._send_panel({}, packet)
        assert calls == [{'action': 'state'}], 'active welcome reaction yields to motion/notice work'
        calls.clear(); device.clear()
        service._send_panel({}, packet)
        assert calls == [{'action': 'state'}, packet], 'AI summary generation must not pause PC heartbeats'
        service.interactions.current_action = None
        calls.clear()
        service._send_panel({}, packet)
        assert calls == [{'action': 'state'}, packet], 'normal telemetry resumes after the motion ends'
    finally:
        service.close()


def test_quiet_presence_keeps_panel_fresh_but_photo_transport_remains_exclusive(tmp_path):
    import sqlite3
    from toolbox.features.stackchan import DeskService
    calls = []
    state = {'interaction': {'pending': {'id': 'find', 'kind': 'presence'}, 'busy': False}}
    service = DeskService(SimpleNamespace(data_dir=tmp_path), sender=lambda config, packet: calls.append(packet) or state)
    root = tmp_path / 'companion'
    (root / 'logs').mkdir(parents=True)
    config = {'companion_dir': str(root)}
    packet = {'v': 1}
    try:
        service.interactions.capture_active = True  # Entire long observation window.
        service._send_panel(config, packet)
        assert calls == [{'action': 'state'}, packet], 'quiet finding must not age out the task page'
        calls.clear()
        with sqlite3.connect(root / 'logs/task-notices.sqlite3') as camera:
            camera.execute('CREATE TABLE notices(id)')
            camera.execute('BEGIN IMMEDIATE')
            assert service._send_panel(config, packet) is None
            assert not calls, 'actual camera frame must exclusively own the existing transport lock'
            camera.rollback()
        state['interaction']['busy'] = True
        service._send_panel(config, packet)
        assert calls == [{'action': 'state'}], 'motion still yields before decoding another atlas'
    finally:
        service.close()
