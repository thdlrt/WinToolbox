import json
from types import SimpleNamespace

import pytest

from toolbox.features.stackchan_dsh import DshSessions, normalize, pending_approval, terminal_event
from toolbox.features.stackchan_sessions import AgentSessions
from toolbox.features.stackchan_attention import WaitingNotices
from toolbox.features.stackchan import DEFAULTS, DeskService


def item(running=False, seq=10, questions=None, session='same', turn=1):
    return {'sessionId': session, 'agentAvailable': True, 'running': running, 'updatedAt': 1792690000000,
            'projections': {'asOfSeq': seq, 'values': {'title': '中文任务',
                'userQuestions': {'active': questions or [], 'settled': ['old']},
                'turnOutline': [{'seq': turn, 'prompt': 'test', 'response': ''}]}}}


def page(*events):
    return {'records': [{'type': 'event', 'event': e} for e in events]}


def end(kind='completed', seq=20):
    return {'type': 'turn/end', 'seq': seq, 'data': {'reason': {'kind': kind}}}


def test_native_questions_namespaces_and_ms_timestamps():
    rows = normalize({'items': [item(True), item(True, questions=['q'], session='waiting'),
                                {**item(), 'blank': True}]}, now=100)['rows']
    assert [r['state'] for r in rows] == ['running', 'waiting']
    assert rows[0]['id'] == 'dsh:same' and rows[0]['title'] == '[DSH] 中文任务'
    assert rows[0]['updated_at'] == 1792690000
    assert normalize({'items': [{**item(), 'agentAvailable': False,
                               'projections': {'values': {'userQuestions': {'active': ['stale']}}}}]})['rows'][0]['state'] == 'idle'


def test_only_explicit_completed_end_is_green_and_ignores_reasoning():
    response = {'type': 'assistant/message', 'data': {'message': {'content': [
        {'type': 'reasoning', 'text': 'private reasoning'}, {'type': 'text', 'text': '构建通过。'}]}}}
    assert terminal_event(page(response, end()), 1) == {'seq': 20, 'kind': 'done', 'summary': '构建通过。'}
    assert terminal_event(page(end('cancelled')), 1) is None
    assert terminal_event(page(end('unknown')), 1) is None
    assert terminal_event(page(end()), 21) is None
    assert terminal_event(page(end('error')), 1)['kind'] == 'problem'
    response['data']['message']['content'] = [{'type': 'text', 'text': 'API key: sk-test-secret-value'}]
    assert 'sk-test' not in terminal_event(page(response, end()), 1)['summary']


def test_permission_wait_requires_unresolved_audit_pair():
    asked = {'type': 'approval/asked', 'data': {'id': 'a'}}
    decided = {'type': 'approval/decided', 'data': {'id': 'a'}}
    assert pending_approval(page(asked))
    assert not pending_approval(page(asked, decided))


def test_lifecycle_skips_old_history_then_confirms_new_turn_end(tmp_path):
    client = DshSessions(home=tmp_path)
    current, calls = [item()], []
    def rpc(method, args):
        calls.append(method)
        return {'items': current} if method == 'session/list' else page(end())
    client.rpc = rpc
    assert 'completion' not in client.read()['rows'][0]
    assert calls == ['session/list']
    current[0] = item(True, seq=15)
    assert client.read()['rows'][0]['state'] == 'running'
    current[0] = item(False, seq=20)
    row = client.read()['rows'][0]
    assert row['completion']['seq'] == 20
    assert client.read()['rows'][0]['completion'] == row['completion']
    current[0] = item(True, seq=30, turn=25)
    assert 'completion' not in client.read()['rows'][0]


def test_archived_sessions_hidden_without_changing_registry(tmp_path):
    folder = tmp_path / 'storages'
    folder.mkdir()
    path = folder / 'workspace.json'
    original = json.dumps({'global': {'archivedSessionIds': ['same']}})
    path.write_text(original)
    client = DshSessions(home=tmp_path)
    client.rpc = lambda *a: {'items': [item(), item(session='other')]}
    assert [r['id'] for r in client.read()['rows']] == ['dsh:other']
    assert path.read_text() == original


def test_auth_uses_existing_pin_and_rpc_cannot_send_or_approve(tmp_path):
    folder = tmp_path / 'dsh-pocket'
    folder.mkdir()
    (folder / 'token-lan').write_text('test-pin')
    client = DshSessions(home=tmp_path)
    calls = []
    def request(method, path, body=None, content_type=None):
        calls.append((method, path, body))
        if path.startswith('/api/'):
            return 200, json.dumps({'rpcId': 'stackchan-read', 'result': {'ok': True, 'value': {'items': []}}}).encode()
        client.cookies['fixture-cookie'] = 'fixture-value'
        return (302 if path == '/pocket-login' else 200), b''
    client._request = request
    assert client.read()['rows'] == []
    assert calls[0][:2] == ('POST', '/pocket-login') and 'test-pin' in calls[0][2]
    assert calls[-1][1] == '/api/session/list'
    with pytest.raises(ValueError):
        client.rpc('session/prompt', {})
    with pytest.raises(ValueError):
        client.rpc('approval/respond', {})


def test_one_source_failure_keeps_other_fresh_and_ids_distinct():
    dsh = SimpleNamespace(port=3081, cookies={}, read=lambda: normalize({'items': [item(True)]}))
    def unavailable(_):
        raise TimeoutError('fixture')
    reader = AgentSessions(codex=unavailable, dsh=dsh)
    value = reader.read(DEFAULTS)
    assert value['available'] and not value['error'] and value['running'] == 1
    assert not value['sources']['codex']['available'] and value['sources']['dsh']['available']
    reader.codex = lambda _: {'available': True, 'rows': [{'id': 'same', 'title': '任务', 'state': 'idle', 'updated_at': 1}]}
    reader.retry_at.clear()
    value = reader.read(DEFAULTS)
    assert {r['id'] for r in value['rows']} == {'same', 'dsh:same'}
    dsh.read = lambda: (_ for _ in ()).throw(TimeoutError())
    value = reader.read(DEFAULTS)
    assert value['available'] and value['waiting'] == 0
    assert [r['id'] for r in value['rows']] == ['same']  # no stale red/running DSH ghosts


def test_done_and_question_delivery_are_separate_and_persist_dedup(tmp_path):
    sent = []
    done = {'id': 'dsh:a', 'title': '[DSH] 任务', 'state': 'idle', 'source': 'dsh',
            'completion': {'seq': 20, 'kind': 'done', 'summary': '完成了本轮。'}}
    waiting = {'id': 'dsh:b', 'title': '[DSH] 等待', 'state': 'waiting', 'source': 'dsh'}
    deliver = lambda rows, config: sent.append(rows) or 'acknowledged'
    agent = WaitingNotices(tmp_path, lambda: True, deliver=deliver)
    snapshot = {'available': True, 'rows': [done, waiting]}
    agent.observe(snapshot, {})
    agent.worker.join(2)
    assert len(sent) == 2
    assert sent[0][0]['notice_kind'] == 'done' and sent[1][0]['state'] == 'waiting'
    restored = WaitingNotices(tmp_path, lambda: True, deliver=deliver)
    restored.observe(snapshot, {})
    assert len(sent) == 2 and restored.worker is None
    done['completion'] = {**done['completion'], 'seq': 30}
    restored.observe(snapshot, {})
    restored.worker.join(2)
    assert len(sent) == 3 and sent[-1][0]['completion']['seq'] == 30


def test_dsh_settings_validate_port_and_boolean(tmp_path):
    service = DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        assert service.save({'dsh_enabled': False, 'dsh_port': 3082})['settings']['dsh_port'] == 3082
        for patch in ({'dsh_port': 0}, {'dsh_port': True}, {'dsh_enabled': 1}):
            with pytest.raises(ValueError):
                service.save(patch)
    finally:
        service.close()
