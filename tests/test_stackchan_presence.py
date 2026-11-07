import base64
import threading
import time
from types import SimpleNamespace

import pytest

from toolbox.features import stackchan
from toolbox.features.stackchan_interaction import Interactions, Presence, photo_png


def test_failed_capture_is_unknown_and_cannot_accumulate_absence():
    p = Presence()
    p.update(False, 100, [])
    p.update(None, 130, [])
    p.update(False, 160, [])
    assert not p.snapshot()['is_away']
    p.update(False, 10010, [])
    p.update(False, 10100, [])
    assert not p.snapshot()['is_away'], 'a long camera gap cannot count as continuous absence'
    assert p.snapshot()['misses'] == 1
    p.update(True, 180, [])
    assert p.snapshot()['state'] == 'present'
    assert p.update(True, 10000, []) is None, 'watching without keyboard input stays present'
    assert not p.snapshot()['is_away']


def make_interaction(tmp_path, *, face=None, failure=False):
    calls = []
    config = dict(stackchan.DEFAULTS, presence_enabled=True)
    def send(settings, payload):
        calls.append(payload)
        if payload['action'] == 'display':
            return {'display': {'off': payload['off'], 'requested_off': payload['off'], 'reason': payload['reason']}}
        return {'accepted': True}
    def rpc(settings, name, args, timeout):
        calls.append(name)
        if failure:
            raise TimeoutError('camera timeout')
        assert name == 'camera_take_photo'
        return {'result': {'content': [{'type': 'image', 'data': base64.b64encode(b'fixture').decode()}]}}
    service = SimpleNamespace(app=SimpleNamespace(data_dir=tmp_path), closed=False, config=config,
                              send=send, robot_call=rpc, wake=threading.Event())
    a = Interactions(service, presence_detector=lambda _: face)
    a.configured = tuple(config[k] for k in ('interaction_enabled', 'pet_enabled', 'search_enabled', 'motion_seconds', 'camera_mirror', 'presence_absence_seconds'))
    return a, config, calls


def wait_idle(a):
    deadline = time.perf_counter() + 2
    while a.busy and time.perf_counter() < deadline:
        time.sleep(.005)
    assert not a.busy
    assert not a.status().get('error'), a.status()


def test_camera_period_is_30_seconds_and_disabled_interaction_never_moves_motor(tmp_path, monkeypatch):
    a, config, calls = make_interaction(tmp_path, face={'x': .2, 'y': 0})
    config['interaction_enabled'] = False
    a.configured = tuple(config[k] for k in ('interaction_enabled', 'pet_enabled', 'search_enabled', 'motion_seconds', 'camera_mirror', 'presence_absence_seconds'))
    clock = [100]
    monkeypatch.setattr('toolbox.features.stackchan_interaction.time.monotonic', lambda: clock[0])
    answer = {'interaction': {}}
    a.tick(config, {}, answer); wait_idle(a)
    assert a.status()['presence']['state'] == 'present'
    for value in (101, 115, 129.9):
        clock[0] = value
        a.tick(config, {}, answer); wait_idle(a)
    assert calls == ['camera_take_photo']
    clock[0] = 130
    a.tick(config, {}, answer); wait_idle(a)
    assert calls == ['camera_take_photo', 'camera_take_photo']
    assert (tmp_path / 'stackchan-presence.json').is_file()


def test_unknown_capture_keeps_display_on_and_reports_reason(tmp_path):
    a, config, calls = make_interaction(tmp_path, failure=True)
    a.tick(config, {}, {'interaction': {}}); wait_idle(a)
    assert a.status()['presence']['state'] == 'unknown'
    assert not a.status()['presence']['is_away']
    assert 'camera timeout' in a.status()['presence_error']
    assert calls == ['camera_take_photo']


def test_manual_screen_modes_override_camera_and_auto_returns_to_presence(tmp_path):
    a, config, calls = make_interaction(tmp_path)
    a.next_presence = float('inf')
    a.presence.update(False, 100, []); a.presence.update(False, 160, [])
    answer = {'interaction': {}, 'display': {'requested_off': False, 'reason': 'present'}}
    a.tick(config, {}, answer); wait_idle(a)
    assert calls[-1] == {'action': 'display', 'off': True, 'reason': 'away'}
    config['screen_mode'] = 'on'
    a.tick(config, {}, {'interaction': {}, 'display': {'requested_off': True, 'reason': 'away'}}); wait_idle(a)
    assert calls[-1] == {'action': 'display', 'off': False, 'reason': 'manual'}
    config['screen_mode'] = 'off'
    a.presence.update(True, 170, [])
    a.tick(config, {}, answer); wait_idle(a)
    assert calls[-1] == {'action': 'display', 'off': True, 'reason': 'manual'}
    config['screen_mode'] = 'auto'
    a.tick(config, {}, {'interaction': {}, 'display': {'requested_off': True, 'reason': 'manual'}}); wait_idle(a)
    assert calls[-1] == {'action': 'display', 'off': False, 'reason': 'present'}


def test_camera_switch_off_stops_sampling_and_resets_away(tmp_path):
    a, config, calls = make_interaction(tmp_path)
    config['presence_enabled'] = False
    a.presence.update(False, 100, []); a.presence.update(False, 160, [])
    a.tick(config, {}, {'interaction': {}})
    assert not a.busy and not calls
    assert a.status()['presence']['state'] == 'unknown'


def test_presence_settings_validation_and_partial_save(tmp_path, monkeypatch):
    monkeypatch.setattr(stackchan.DeskService, 'start', lambda _: None)
    service = stackchan.DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        for value in ({'presence_enabled': 'yes'}, {'screen_mode': 'bad'}, {'presence_absence_seconds': 0},
                      {'presence_absence_seconds': True}):
            with pytest.raises(ValueError):
                service.save(value)
        service.save({'presence_enabled': True, 'presence_absence_seconds': 120, 'city': '南京'})
        value = service.save({'screen_mode': 'off'})['settings']
        assert value['presence_enabled'] and value['presence_absence_seconds'] == 120
        assert value['city'] == '南京' and value['screen_mode'] == 'off'
    finally:
        service.close()


def searching_interaction(tmp_path, monkeypatch, faces):
    from toolbox.features import stackchan_interaction as module
    a, config, calls = make_interaction(tmp_path)
    detections = iter(faces)
    a.presence_detector = lambda _: next(detections, None)
    pending = {}
    def send(settings, payload):
        calls.append(payload)
        if payload['op'] == 'presence':
            pending.update(id=payload['id'], remaining_ms=75000)
        return {'accepted': True, 'pending': dict(pending), 'capture_allowed': True, 'search': True}
    a.service.send = send
    clock = [1000.]
    a._test_clock = clock
    # These fixtures exercise the bounded search after the first missed sample.
    a.face_misses = 1
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return a, config, calls


def test_lost_face_observes_full_minute_before_away_and_never_repeats_search(tmp_path, monkeypatch):
    a, config, calls = searching_interaction(tmp_path, monkeypatch, [None] * 30)
    original_send = a.service.send
    def send(settings, payload):
        if payload['op'] == 'seek':
            assert a.presence.away is None, 'screen stays awake during confirmation'
        return original_send(settings, payload)
    a.service.send = send
    a._presence(config, {})
    assert a._test_clock[0] >= 1060
    assert a.presence.snapshot()['is_away'] and a.absence_searched
    assert 3 < calls.count('camera_take_photo') <= 12
    assert sum(isinstance(c, dict) and c['op'] == 'seek' for c in calls) == 2
    before = len(calls)
    for _ in range(3):
        a._test_clock[0] += 30
        a._presence(config, {})
    assert calls[before:] == ['camera_take_photo'] * 3, 'away checks never rescan the room'


def test_face_found_during_search_cancels_absence_and_centers_from_cached_sample(tmp_path, monkeypatch):
    a, config, calls = searching_interaction(tmp_path, monkeypatch, [None, {'x': .4, 'y': -.1}, {'x': .5, 'y': 0}])
    a._presence(config, {})
    assert calls.count('camera_take_photo') == 2
    assert a.presence.snapshot()['state'] == 'present' and a.presence.absent_since is None
    assert not a.absence_searched
    assert calls[-1]['op'] == 'target' and calls[-1]['face'] == {'x': .4, 'y': -.1}
    a._presence(config, {})
    assert calls.count('camera_take_photo') == 3
    assert calls[-1] == 'camera_take_photo', 'a found face stays still on subsequent presence checks'


def test_interrupted_search_does_not_count_as_valid_absence(tmp_path, monkeypatch):
    a, config, calls = searching_interaction(tmp_path, monkeypatch, [None])
    original = a.service.send
    def send(settings, payload):
        if payload['op'] == 'state':
            return {'pending': {'id': 'new-problem'}, 'accepted': False}
        return original(settings, payload)
    a.service.send = send
    a._presence(config, {})
    assert a.presence.snapshot()['state'] == 'unknown'
    assert a.presence.absent_since is None and not a.absence_searched
    assert calls.count('camera_take_photo') == 1


def test_return_already_oriented_does_not_move_again(tmp_path, monkeypatch):
    from toolbox.features import stackchan_interaction as module
    a, config, calls = make_interaction(tmp_path)
    a.service.robot_call = lambda *_, **__: {'result': {'content': [{'type': 'text', 'text': '{"status":"expired"}'}]}}
    monkeypatch.setattr(module, 'away_records', lambda *args: [])
    a._welcome(config, {'id': 'return', 'since': 1, 'baseline': {}, 'oriented': True}, {})
    assert not calls and a.status()['status'] == 'welcome-no-new-information'


def test_pet_found_face_wakes_presence_and_preserves_return_summary(tmp_path):
    a, config, calls = make_interaction(tmp_path)
    a.presence.update(False, 100, [])
    a.presence.update(False, 160, [])
    a._orient = lambda *args: {'complete': True, 'found': True, 'face': {'x': .3, 'y': 0}}
    a._run('orient', {'id': 'pet-1', 'kind': 'pet'}, config, {})
    assert a.presence.snapshot()['state'] == 'present'
    assert not a.presence.snapshot()['is_away']
    assert a.return_pending['oriented'] and a.return_pending['face_point']['x'] == .3


def test_face_loss_starts_bounded_observation_and_only_then_sleeps(tmp_path, monkeypatch):
    a, config, calls = searching_interaction(tmp_path, monkeypatch, [{'x': .3, 'y': 0}, None])
    a._presence(config, {})
    assert a.facing
    a._test_clock[0] += config['search_cooldown_seconds']
    a._presence(config, {})
    assert a.facing and a.presence.away is None, 'one missed sample cannot start a motor search'
    a._test_clock[0] += 30
    start = a._test_clock[0]
    a._presence(config, {})
    assert a._test_clock[0] - start >= 60
    assert not a.facing and a.absence_searched
    assert a.presence.state == 'away'
    assert sum(isinstance(c, dict) and c['op'] == 'seek' for c in calls) == 2


def test_single_face_miss_and_recovery_never_move(tmp_path, monkeypatch):
    a, config, calls = searching_interaction(tmp_path, monkeypatch, [None, {'x': .2, 'y': 0}])
    a.face_misses = 0
    a.facing = True
    a._presence(config, {})
    assert calls == ['camera_take_photo']
    a._test_clock[0] += 30
    a._presence(config, {})
    assert calls == ['camera_take_photo'] * 2 and a.facing


def test_failed_auto_search_obeys_cooldown_but_pet_bypasses_it(tmp_path, monkeypatch):
    a, config, calls = searching_interaction(tmp_path, monkeypatch, [None] * 10)
    a._orient = lambda *_, **__: {'complete': False, 'found': False, 'face': None}
    a._presence(config, {})
    assert a.status()['presence']['motion_cooldown_remaining'] == 300
    before = len(calls)
    for _ in range(4):
        a._test_clock[0] += 30
        a._presence(config, {})
    assert calls[before:] == ['camera_take_photo'] * 4
    invoked = []
    original_send = a.service.send
    a.service.send = lambda settings, payload: {'display': {'off': False}} if payload['action'] == 'display' else original_send(settings, payload)
    a._orient = lambda *_, **__: invoked.append(True)
    a._run('orient', {'id': 'pet-now', 'kind': 'pet'}, config, {})
    assert invoked, 'explicit head touch can search during automatic cooldown'


def test_auto_screen_off_yields_to_notice_motion_and_unknown_camera(tmp_path):
    a, config, calls = make_interaction(tmp_path)
    a.next_presence = float('inf')
    a.presence.confirm_away(200, [], 60)
    for active in ({'screen': 'task-notice'}, {'interaction': {'busy': True}},
                   {'interaction': {'pending': {'id': 'p', 'kind': 'pet'}}}):
        answer = {'interaction': {}, 'display': {'requested_off': True, 'reason': 'away'}, **active}
        if active.get('interaction', {}).get('pending'):
            a.seen = ['p']
        a.tick(config, {}, answer); wait_idle(a)
        assert calls[-1] == {'action': 'display', 'off': False, 'reason': 'present'}
    a.presence.update(None, 210, [])
    a.tick(config, {}, {'interaction': {}, 'display': {'requested_off': True, 'reason': 'away'}}); wait_idle(a)
    assert calls[-1]['off'] is False


def test_pet_wakes_before_localization_even_when_face_is_not_found(tmp_path):
    a, config, calls = make_interaction(tmp_path)
    a.presence.confirm_away(200, [], 60)
    def orient(*args):
        assert a.presence.state == 'present' and a.presence.away is None
        assert calls[-1] == {'action': 'display', 'off': False, 'reason': 'present'}
        return {'complete': False, 'found': False, 'face': None}
    a._orient = orient
    a._run('orient', {'id': 'pet-1', 'kind': 'pet'}, config, {})
    assert a.presence.state == 'present'


def test_raw_luma_is_wrapped_on_pc_with_identical_pixels_and_strict_size():
    import cv2
    import numpy as np
    image = np.arange(160*120, dtype=np.uint8).reshape(120,160)
    resource = {'mimeType': 'application/x-stackchan-gray8', 'blob': base64.b64encode(image.tobytes()).decode()}
    response = {'result': {'content': [{'type':'resource', 'resource':resource}]}}
    png, encoding = photo_png(response)
    assert encoding == 'gray8'
    assert np.array_equal(cv2.imdecode(np.frombuffer(png,dtype=np.uint8),cv2.IMREAD_GRAYSCALE), image)
    resource['blob'] = base64.b64encode(b'bad').decode()
    with pytest.raises(ValueError, match='尺寸'):
        photo_png(response)


@pytest.mark.parametrize('change', [{'presence_enabled': False}, {'screen_mode': 'off'}])
def test_disabling_presence_or_manual_override_cancels_window_without_sleeping(tmp_path, monkeypatch, change):
    a, config, calls = searching_interaction(tmp_path, monkeypatch, [None])
    original = a.service.send
    def send(settings, payload):
        if payload['op'] == 'state':
            a.service.config = {**config, **change}
        return original(settings, payload)
    a.service.send = send
    a._presence(config, {})
    assert a.presence.state == 'unknown'
    assert not a.presence.snapshot()['is_away']
    assert calls.count('camera_take_photo') == 1


def test_presence_stays_busy_until_terminal_snapshot_is_saved(tmp_path, monkeypatch):
    import json
    from toolbox.features import stackchan_interaction
    a, config, calls = make_interaction(tmp_path, face={'x': .2, 'y': 0})
    config['interaction_enabled'] = False
    a.configured = tuple(config[k] for k in ('interaction_enabled', 'pet_enabled', 'search_enabled', 'motion_seconds', 'camera_mirror', 'presence_absence_seconds'))
    entered, release = threading.Event(), threading.Event()
    original_save = stackchan_interaction.atomic_json
    def delayed_save(path, snapshot):
        assert snapshot['busy'] is False and snapshot['current_action'] is None
        entered.set()
        assert release.wait(2)
        original_save(path, snapshot)
    monkeypatch.setattr(stackchan_interaction, 'atomic_json', delayed_save)
    a.tick(config, {}, {'interaction': {}})
    try:
        assert entered.wait(2)
        assert a.status()['busy'] is True
        a.next_presence = 0
        a.tick(config, {}, {'interaction': {}})
        assert calls == ['camera_take_photo']
    finally:
        release.set()
        wait_idle(a)
    snapshot = json.loads((tmp_path / 'stackchan-presence.json').read_text('utf-8'))
    assert snapshot['busy'] is False and snapshot['current_action'] is None
