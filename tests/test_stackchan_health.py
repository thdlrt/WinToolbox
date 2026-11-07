import json

from toolbox.features.stackchan_health import RobotHealth


def test_uptime_decrease_records_restart_and_survives_backend_restart(tmp_path):
    clock = [1000]
    config = {'host': '192.168.1.2', 'port': 8080}
    health = RobotHealth(tmp_path, lambda: clock[0])
    health.observe({'uptime_seconds': 500, 'page': 'tasks', 'screen': 'task-notice'}, config)
    clock[0] += 30
    assert health.observe({'uptime_seconds': 530}, config)['observed_restarts'] == 0
    # Previous visible state is recorded at the real decreasing sample.
    health.observe({'uptime_seconds': 531, 'page': 'pc', 'screen': 'desktop'}, config)
    clock[0] += 30
    state = health.observe({'uptime_seconds': 15}, config)
    assert state['observed_restarts'] == 1
    assert state['events'][-1]['previous_page'] == 'pc'
    assert state['events'][-1]['reset_cause'] == 'unknown'
    restored = RobotHealth(tmp_path, lambda: clock[0])
    assert restored.snapshot()['observed_restarts'] == 1
    assert restored.observe({'uptime_seconds': 16}, config)['observed_restarts'] == 1


def test_old_firmware_and_invalid_snapshots_never_infer_restart(tmp_path):
    health = RobotHealth(tmp_path)
    config = {'host': '192.168.1.2'}
    for packet in ({}, {'error': 'timeout'}, {'uptime_seconds': None}, {'uptime_seconds': True},
                   {'uptime_seconds': float('nan')}, {'uptime_seconds': -1}):
        assert not health.observe(packet, config)['supported']
    assert not health.path.exists()
    health.observe({'uptime_seconds': 100}, config)
    before = health.snapshot()
    assert health.observe({'error': 'timeout'}, config) == before
    assert health.observe({'uptime_seconds': 99}, config)['observed_restarts'] == 0


def test_target_change_and_tick_wrap_are_not_restarts_and_events_are_bounded(tmp_path):
    health = RobotHealth(tmp_path)
    config = {'host': '192.168.1.2'}
    health.observe({'uptime_seconds': 4294960}, config)
    assert health.observe({'uptime_seconds': 10}, config)['observed_restarts'] == 0
    for _ in range(25):
        health.observe({'uptime_seconds': 100}, config)
        health.observe({'uptime_seconds': 10}, config)
    assert health.snapshot()['observed_restarts'] == 25
    assert len(health.snapshot()['events']) == 20
    assert health.observe({'uptime_seconds': 1}, {'host': '192.168.1.3'})['observed_restarts'] == 0


def test_corrupt_persisted_file_is_ignored(tmp_path):
    (tmp_path / 'stackchan-health.json').write_text(json.dumps({'events': False}), encoding='utf-8')
    assert RobotHealth(tmp_path).snapshot() == {'supported': False, 'observed_restarts': 0, 'events': []}


def test_reboot_keeps_last_runtime_context_without_task_or_camera_content(tmp_path):
    h = RobotHealth(tmp_path)
    config = {'host': '192.168.1.2'}
    h.observe({'uptime_seconds': 100, 'draw_count': 12, 'decode_ms': 400,
               'runtime': {'build': 'stable', 'boot_id': 'old', 'chunk_used': 200000, 'token': 'secret'},
               'interaction': {'busy': True, 'result': 'searched', 'pending': {'kind': 'presence', 'id': 'private'}}}, config)
    value = h.observe({'uptime_seconds': 10, 'runtime': {'boot_id': 'new', 'reset_cause': 'Panic'}}, config)
    event = value['events'][-1]
    assert event['reset_cause'] == 'Panic'
    assert event['previous_diagnostics']['runtime']['boot_id'] == 'old'
    assert event['previous_diagnostics']['pending_kind'] == 'presence'
    assert 'secret' not in json.dumps(value) and 'private' not in json.dumps(value)
