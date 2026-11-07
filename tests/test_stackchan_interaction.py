import base64
import json
import sqlite3
import time
from types import SimpleNamespace
import pytest
from toolbox.features.stackchan_interaction import Presence, away_records, summarize_return, detect_face, Interactions, notice_transport_slot
from toolbox.features.stackchan import DEFAULTS


@pytest.mark.parametrize('final,complete,outcome', [
    ({'busy': False, 'result': 'motion-error', 'motion_error': 'set-pose: servo timeout'}, False, 'motion-error'),
    ({'busy': False, 'result': 'oriented', 'motion_error': 'release-torque: servo timeout'}, False, 'motion-error'),
    ({'busy': False, 'result': 'oriented'}, True, 'face-found'),
    ({'busy': True, 'result': 'searched'}, False, 'motion-unconfirmed'),
])
def test_orientation_waits_for_final_motion_and_bounds_confirmation(monkeypatch, final, complete, outcome):
    from toolbox.features import stackchan_interaction as module
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    config = dict(DEFAULTS)
    event = {'id': 'final-servo-timeout', 'kind': 'welcome'}
    calls = []
    def send(_, payload):
        calls.append(payload['op'])
        if payload['op'] == 'target':
            return {'accepted': True, 'busy': True, 'result': 'searched'}
        return final
    a = Interactions(SimpleNamespace(app=SimpleNamespace(), send=send, config=config, closed=False))
    result = a._orient(config, event,
        {'pending': {'id': event['id']}, 'capture_allowed': True},
        first_frame={'face': {'x': .5, 'y': 0}})
    assert result['complete'] is complete, 'dispatch acceptance alone cannot confirm orientation'
    assert a.status()['orientation'] == outcome
    assert calls[0] == 'target' and calls[1:] and set(calls[1:]) == {'state'}
    assert clock[0] <= 100 + config['motion_seconds'] + 2.21, 'confirmation must stop within motion duration plus two seconds'
    if final.get('motion_error'):
        assert 'servo timeout' in a.status()['capture_error']


def test_return_requires_confirmed_camera_absence_then_face_and_does_not_repeat():
    p = Presence()
    assert p.update(True, 1000, []) is None
    for now in (1100, 1130, 1160):
        assert p.update(False, now, []) is None
    assert p.snapshot()['is_away']
    e = p.update(True, 1165, [])
    assert e['since'] == 1100 and e['welcome_allowed']
    assert p.update(True, 1170, []) is None
    p.update(False, 1200, []); p.update(False, 1260, [])
    assert not p.update(True, 1270, [])['welcome_allowed']
    assert not p.snapshot()['is_away'], 'display still wakes even while welcome is in cooldown'


def test_summary_uses_only_acknowledged_events_since_away(tmp_path):
    (tmp_path / 'logs').mkdir()
    with sqlite3.connect(tmp_path / 'logs/task-notices.sqlite3') as db:
        db.execute('CREATE TABLE notices(task,kind,summary,at,outcome)')
        db.executemany('INSERT INTO notices VALUES(?,?,?,?,?)', [('old','done','old',1,'acknowledged'),
            ('new','done','built',20,'acknowledged'),('bad','problem','offline',21,'unavailable'),
            ('welcome','welcome','hi',22,'acknowledged')])
    r = away_records(tmp_path, 10)
    assert r == [{'task':'new','state':'done','summary':'built'}]
    app = SimpleNamespace(providers=SimpleNamespace(chat=lambda *a, **k: '已完成构建。'))
    assert summarize_return(app,r,[])[1] == 'ai'
    assert summarize_return(app,[],[]) == ('','no-change')
    text, source = summarize_return(SimpleNamespace(), [], [{'state':'idle','title':'task'}])
    assert (text, source) == ('', 'no-change')
    seen = []
    app = SimpleNamespace(providers=SimpleNamespace(chat=lambda messages, **_: seen.append(messages) or '任务仍在运行。'))
    summarize_return(app, [], [{'state': 'idle', 'title': 'do-not-request-action'}, {'state': 'running', 'title': 'active'}])
    assert 'do-not-request-action' not in str(seen)
    assert 'active' in str(seen)


def test_camera_return_reuses_face_and_keeps_full_summary_for_pagination(tmp_path, monkeypatch):
    from toolbox.features import stackchan_interaction as module
    (tmp_path / 'connection.json').write_text(json.dumps({'host': '192.168.1.2', 'port': 8080}))
    calls, commands = [], []
    app = SimpleNamespace(providers=SimpleNamespace(chat=lambda *_, **__: '仍在运行的任务已更新，具体结果请查看电脑。' * 5))
    def rpc(config, name, args, timeout):
        calls.append(name)
        assert name == 'task_notice', 'return must reuse the presence frame'
        return {'result': {'content': [{'type': 'text', 'text': '{"status":"expired"}'}]}}
    service = SimpleNamespace(app=app, send=lambda config, payload: calls.append(payload) or {'accepted': True}, robot_call=rpc)
    monkeypatch.setattr(module, 'away_records', lambda *args: [])
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kwargs: commands.append(command) or SimpleNamespace(stdout='{"status":"acknowledged"}'))
    a = Interactions(service)
    event = {'since': time.time() - 90, 'id': 'camera-return', 'baseline': {},
             'face_checked_at': time.time(), 'face_point': {'x': .2, 'y': -.1}}
    a._welcome(dict(DEFAULTS, companion_dir=str(tmp_path), host='192.168.1.2', port=8080), event,
               {'rows': [{'id': 'r', 'state': 'running', 'updated_at': 1, 'title': 'active'}]})
    assert a.status()['status'] == 'welcomed'
    assert calls[-1] == {'action': 'interaction', 'op': 'target', 'id': 'camera-return', 'face': event['face_point'], 'found': True}
    command = commands[0]
    assert command[command.index('--summary') + 1] == a.status()['summary']
    assert len(a.status()['summary']) > 60, 'all summary text goes to the paginated renderer'
    assert command[command.index('--page-index') + 1] == '0'


def test_welcome_pages_repeat_twice_and_stop_if_dismissed_or_preempted(monkeypatch):
    a = Interactions(SimpleNamespace(app=SimpleNamespace()))
    a.welcome_generation = 1
    waits, pages = [], []
    def page(config, event, summary, index, **kwargs):
        pages.append((event, summary, index, kwargs))
        return {'status': 'acknowledged', 'id': event}
    monkeypatch.setattr(a, '_welcome_wait', lambda *args: waits.append(True) or True)
    monkeypatch.setattr(a, '_welcome_page', page)
    a._welcome_pages({}, 'return', '完整摘要', 3, 'initial-id', 1)
    assert [p[2] for p in pages] == [1, 2, 0, 1, 2]
    assert [p[3]['replace_id'] for p in pages] == ['initial-id'] + [p[0] for p in pages[:-1]]
    assert pages[-1][3]['seconds'] == 8
    assert a.status()['summary_round'] == 2 and a.status()['summary_playback'] == 'finished'
    pages.clear()
    monkeypatch.setattr(a, '_welcome_page', lambda *args, **kwargs: pages.append(args) or {'status': 'deferred'})
    a._welcome_pages({}, 'return', '完整摘要', 3, 'dismissed-id', 1)
    assert len(pages) == 1 and a.status()['summary_playback'] == 'stopped'


def test_welcome_page_dwell_cancels_on_shutdown_replacement_or_manual_off():
    config = dict(DEFAULTS, enabled=True, welcome_enabled=True)
    service = SimpleNamespace(app=SimpleNamespace(), config=config, closed=True)
    a = Interactions(service)
    assert not a._welcome_wait(config, 0)
    service.closed = False
    assert not a._welcome_wait(config, 1)
    service.config = dict(config, screen_mode='off')
    assert not a._welcome_wait(config, 0)


def test_single_photo_no_retry_no_cloud_and_red_notice_restore():
    calls = []
    service = SimpleNamespace(app=SimpleNamespace())
    def send(config, payload):
        calls.append(payload)
        return {'accepted':True}
    service.send = send
    def rpc(config,name,args,timeout):
        calls.append(name)
        return {'result':{'content':[{'type':'image','data':base64.b64encode(b'fixture').decode()}]}}
    service.robot_call = rpc
    a = Interactions(service, face_detector=lambda b: {'x':.3,'y':-.2})
    state = {'pending':{'id':'one'},'capture_allowed':True}
    a._orient({}, {'id':'one'}, state)
    assert calls.count('camera_take_photo') == 1
    assert calls[-1] == {'action':'interaction','op':'target','id':'one','face':{'x':.3,'y':-.2},'found':True}
    calls.clear()
    a._orient({}, {'id':'one'}, {'pending':{'id':'one'},'capture_allowed':False})
    assert calls == [{'action':'interaction','op':'target','id':'one','face':None,'found':False}]
    assert a.status()['orientation'] == 'camera-disarmed'
    calls.clear()
    a._orient({}, {'id':'old'}, state)
    assert not calls, 'stale notice never captures or moves'


def test_local_detector_rejects_unknown_size_and_returns_none_for_blank():
    import cv2
    import numpy as np
    ok, png = cv2.imencode('.png', np.zeros((120,160),dtype=np.uint8))
    assert ok and detect_face(png.tobytes()) is None
    with pytest.raises(ValueError):
        detect_face(b'bad')


def test_telemetry_yields_to_shared_notice_upload_and_releases_on_failure(tmp_path):
    (tmp_path / 'logs').mkdir()
    path = tmp_path / 'logs/task-notices.sqlite3'
    config = {'companion_dir': str(tmp_path)}
    with sqlite3.connect(path, timeout=0) as upload:
        upload.execute('CREATE TABLE notices(id)')
        upload.execute('BEGIN IMMEDIATE')
        with notice_transport_slot(config) as acquired:
            assert not acquired, 'telemetry must skip while another process uploads a card'
        upload.rollback()
        with pytest.raises(RuntimeError):
            with notice_transport_slot(config) as acquired:
                assert acquired
                with pytest.raises(sqlite3.OperationalError):
                    upload.execute('BEGIN IMMEDIATE')
                raise RuntimeError('transport failed')
        upload.execute('BEGIN IMMEDIATE')
        upload.rollback()


def test_background_tick_configures_then_consumes_problem_once(tmp_path):
    calls = []
    event = {'id': 'live-problem', 'kind': 'problem'}
    def send(settings, payload):
        calls.append(payload)
        if payload['op'] == 'state':
            return {'pending': event, 'capture_allowed': True}
        return {'accepted': True}
    def rpc(settings, name, args, timeout):
        calls.append(name)
        return {'result': {'content': [{'type': 'image', 'data': base64.b64encode(b'fixture').decode()}]}}
    service = SimpleNamespace(app=SimpleNamespace(data_dir=tmp_path), closed=False, send=send, robot_call=rpc)
    a = Interactions(service, face_detector=lambda _: {'x': .3, 'y': -.2})
    config = dict(DEFAULTS)
    answer = {'interaction': {'pending': event}}
    def wait_idle():
        deadline = time.monotonic() + 2
        while a.busy and time.monotonic() < deadline:
            time.sleep(.005)
        assert not a.busy
        assert 'error' not in a.status(), a.status()
    a.tick(config, {}, answer)
    wait_idle()
    assert a.configured is not None, 'configure must complete before processing notices'
    assert calls[0] == {'action': 'interaction', 'op': 'configure', 'config': {
        'enabled': True, 'pet': True, 'search': True, 'mirror': False, 'seconds': 3.5, 'presence_seconds': 60}}
    a.tick(config, {}, answer)
    wait_idle()
    a.tick(config, {}, answer)
    wait_idle()
    assert calls.count('camera_take_photo') == 1
    assert sum(isinstance(c, dict) and c['op'] == 'target' for c in calls) == 1
    assert a.status()['orientation'] == 'face-found'


@pytest.mark.parametrize('found_at,photos,seeks', [(0,1,0),(1,2,1),(2,3,2),(None,3,2)])
def test_finite_search_stops_on_face_or_after_two_views(monkeypatch, found_at, photos, seeks):
    monkeypatch.setattr('toolbox.features.stackchan_interaction.time.sleep', lambda _: None)
    calls = []
    pending = {'id': 'search', 'remaining_ms': 30000}
    state = {'pending': pending, 'capture_allowed': True, 'search': True}
    def send(config, payload):
        calls.append(payload)
        return state | {'accepted': True}
    def rpc(config, name, args, timeout):
        calls.append(name)
        return {'result': {'content': [{'type': 'image', 'data': base64.b64encode(b'fixture').decode()}]}}
    detections = iter([None if i != found_at else {'x': .4, 'y': -.1} for i in range(3)])
    service = SimpleNamespace(app=SimpleNamespace(), send=send, robot_call=rpc)
    a = Interactions(service, face_detector=lambda _: next(detections))
    a._orient(dict(DEFAULTS), {'id': 'search'}, state)
    assert calls.count('camera_take_photo') == photos
    assert sum(isinstance(c,dict) and c['op']=='seek' for c in calls) == seeks
    assert calls[-1]['op'] == 'target' and calls[-1]['found'] == (found_at is not None)
    assert not a.capture_active and a.status()['photos'] == photos


def test_search_stops_if_notice_is_replaced(monkeypatch):
    monkeypatch.setattr('toolbox.features.stackchan_interaction.time.sleep', lambda _: None)
    calls = []
    start = {'pending': {'id':'old','remaining_ms':30000},'capture_allowed':True,'search':True}
    def send(config,payload):
        calls.append(payload)
        return {'pending':{'id':'new','remaining_ms':30000},'accepted':False}
    def rpc(config,name,args,timeout):
        calls.append(name)
        return {'result':{'content':[{'type':'image','data':base64.b64encode(b'fixture').decode()}]}}
    a = Interactions(SimpleNamespace(app=SimpleNamespace(),send=send,robot_call=rpc),face_detector=lambda _:None)
    a._orient(dict(DEFAULTS),{'id':'old'},start)
    assert calls.count('camera_take_photo') == 1
    assert not any(isinstance(c,dict) and c['op']=='seek' for c in calls)
    assert a.status()['orientation'] == 'stale-event-skipped'


def test_search_does_not_capture_or_continue_after_motor_failure(monkeypatch):
    monkeypatch.setattr('toolbox.features.stackchan_interaction.time.sleep', lambda _: None)
    calls = []
    start = {'pending': {'id': 'motor', 'remaining_ms': 30000}, 'capture_allowed': True, 'search': True}
    def send(config, payload):
        calls.append(payload)
        return start | {'accepted': True, 'result': 'motion-error', 'motion_error': 'set-pose: servo timeout'}
    def rpc(config, name, args, timeout):
        calls.append(name)
        return {'result': {'content': [{'type': 'image', 'data': base64.b64encode(b'fixture').decode()}]}}
    a = Interactions(SimpleNamespace(app=SimpleNamespace(), send=send, robot_call=rpc), face_detector=lambda _: None)
    a._orient(dict(DEFAULTS), {'id': 'motor'}, start)
    assert calls.count('camera_take_photo') == 1
    assert sum(isinstance(c, dict) and c['op'] == 'seek' for c in calls) == 1
    assert a.status()['orientation'] == 'motion-error'
    assert a.status()['capture_error'] == 'set-pose: servo timeout'


def test_device_reboot_reapplies_saved_speed_even_with_unchanged_pc_settings(tmp_path):
    calls = []
    service = SimpleNamespace(app=SimpleNamespace(data_dir=tmp_path), closed=False,
                              send=lambda settings, payload: calls.append(payload) or {})
    a = Interactions(service)
    config = dict(DEFAULTS, motion_seconds=2)
    a.configured = tuple(config[k] for k in ('interaction_enabled', 'pet_enabled', 'search_enabled', 'motion_seconds', 'camera_mirror'))
    a.tick(config, {}, {'interaction': {'seconds': 3.5, 'enabled': True, 'pet': True, 'search': True}})
    deadline = time.monotonic() + 2
    while a.busy and time.monotonic() < deadline:
        time.sleep(.005)
    assert not a.busy
    assert calls[0]['op'] == 'configure' and calls[0]['config']['seconds'] == 2
    a.tick(config, {}, {'interaction': {'seconds': 2, 'enabled': True, 'pet': True, 'search': True}})
    assert len(calls) == 1, 'matching runtime config does not resubmit every tick'


def test_short_torque_recovery_is_waited_before_next_photo(monkeypatch):
    monkeypatch.setattr('toolbox.features.stackchan_interaction.time.sleep', lambda _: None)
    state = {'pending': {'id': 'recover', 'remaining_ms': 30000}, 'capture_allowed': True, 'search': True}
    busy_reads, photos = [False, True, False], []
    def send(config, payload):
        busy = busy_reads.pop(0) if payload['op'] == 'state' and busy_reads else False
        return state | {'accepted': True, 'busy': busy}
    def rpc(config, name, args, timeout):
        photos.append(name)
        return {'result': {'content': [{'type': 'image', 'data': base64.b64encode(b'fixture').decode()}]}}
    detections = iter([None, {'x': .3, 'y': -.1}])
    a = Interactions(SimpleNamespace(app=SimpleNamespace(), send=send, robot_call=rpc), face_detector=lambda _: next(detections))
    a._orient(dict(DEFAULTS), {'id': 'recover'}, state)
    assert len(photos) == 2 and a.status()['orientation'] == 'face-found'
