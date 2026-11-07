import json
import sqlite3
from types import SimpleNamespace

from toolbox.features.stackchan_attention import WaitingNotices


def sample(*rows):
    return {'available': True, 'rows': list(rows)}


def row(thread='a', state='waiting', updated=1):
    return {'id': thread, 'state': state, 'title': '任务' + thread, 'updated_at': updated}


def finish(agent):
    if agent.worker:
        agent.worker.join(2)
        assert not agent.worker.is_alive()


def test_wait_episode_once_across_updates_and_restart_then_new_wait(tmp_path):
    sent = []
    deliver = lambda rows, config: sent.append(rows) or 'acknowledged'
    agent = WaitingNotices(tmp_path, lambda: True, deliver=deliver)
    agent.observe(sample(row()), {}); finish(agent)
    agent.observe(sample(row(updated=99)), {}); finish(agent)
    assert len(sent) == 1
    restarted = WaitingNotices(tmp_path, lambda: True, deliver=deliver)
    restarted.observe(sample(row(updated=100)), {}); finish(restarted)
    assert len(sent) == 1
    restarted.observe(sample(row(state='active')), {})
    restarted.observe(sample(row(updated=101)), {}); finish(restarted)
    assert len(sent) == 2


def test_multiple_sessions_grouped_without_repeating_or_using_idle_as_done(tmp_path):
    sent = []
    agent = WaitingNotices(tmp_path, lambda: True, deliver=lambda rows, _: sent.append(rows) or 'acknowledged')
    agent.observe(sample(row('a'), row('b'), row('c', 'idle')), {}); finish(agent)
    assert len(sent) == 1 and [r['id'] for r in sent[0]] == ['a', 'b']
    agent.observe({'available': False, 'rows': []}, {})
    agent.observe({'available': True, 'error': 'offline', 'rows': [row('d')]}, {})
    agent.observe(sample(row('a'), row('b')), {}); finish(agent)
    assert len(sent) == 1


def test_recent_agent_notice_prevents_second_backend_popup(tmp_path):
    root = tmp_path / 'companion'
    (root / 'logs').mkdir(parents=True)
    with sqlite3.connect(root / 'logs/task-notices.sqlite3') as db:
        db.execute('CREATE TABLE notices(thread_id, kind, outcome, at)')
        db.execute("INSERT INTO notices VALUES ('a','problem','acknowledged',95)")
    sent = []
    agent = WaitingNotices(tmp_path, lambda: True, clock=lambda: 100,
                           deliver=lambda rows, _: sent.append(rows) or 'acknowledged')
    agent.observe(sample(row('a'), row('b')), {'companion_dir': str(root)}); finish(agent)
    assert len(sent) == 1 and [r['id'] for r in sent[0]] == ['b']


def test_failed_delivery_is_not_retried_on_every_sample_or_sent_when_disabled(tmp_path):
    sent = []
    agent = WaitingNotices(tmp_path, lambda: True, deliver=lambda rows, _: sent.append(rows) or 'unavailable')
    agent.observe(sample(row()), {}); finish(agent)
    agent.observe(sample(row()), {}); finish(agent)
    assert len(sent) == 1 and agent.status()['last_outcome'] == 'unavailable'
    agent.enabled = lambda: False
    agent.observe(sample(row('b')), {}); finish(agent)
    assert len(sent) == 1


def test_delivery_uses_existing_entry_explicit_target_session_and_no_answer(tmp_path, monkeypatch):
    from toolbox.features import stackchan_attention as module
    (tmp_path / 'connection.json').write_text('{"host":"192.168.1.2","port":8080}')
    commands = []
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kw:
                        commands.append((command, kw)) or SimpleNamespace(stdout='{"status":"acknowledged"}'))
    agent = WaitingNotices(tmp_path / 'data', lambda: True, clock=lambda: 100)
    agent.observe(sample(row()), {'companion_dir': str(tmp_path), 'host': '192.168.1.2', 'port': 8080}); finish(agent)
    command, kwargs = commands[0]
    assert command[command.index('--thread-id') + 1] == 'a'
    assert command[command.index('--status') + 1] == 'problem'
    assert command[command.index('--seconds') + 1] == '120'
    assert str(tmp_path / 'task_notify.py') in command
    assert kwargs['timeout'] == 22 and kwargs['env']['PYTHONIOENCODING'] == 'utf-8'


def test_service_only_observes_fresh_successful_session_snapshots(tmp_path):
    import threading
    import time
    from toolbox.features.stackchan import DeskService
    service = DeskService(SimpleNamespace(data_dir=tmp_path))
    calls, observed = [], threading.Event()
    def observe(snapshot, config):
        calls.append(snapshot)
        observed.set()
    service.attention = SimpleNamespace(observe=observe)
    try:
        service._slow_sample('sessions', lambda: sample(row()), service.revision)
        assert observed.wait(2) and len(calls) == 1
        deadline = time.monotonic() + 2
        while service.slow_busy and time.monotonic() < deadline:
            time.sleep(.005)
        observed.clear()
        service._slow_sample('sessions', lambda: sample(row('b')), service.revision - 1)
        deadline = time.monotonic() + 2
        while service.slow_busy and time.monotonic() < deadline:
            time.sleep(.005)
        assert len(calls) == 1
        def fail():
            raise TimeoutError('fixture')
        service._slow_sample('sessions', fail, service.revision)
        deadline = time.monotonic() + 2
        while service.slow_busy and time.monotonic() < deadline:
            time.sleep(.005)
        assert len(calls) == 1
    finally:
        service.close()
