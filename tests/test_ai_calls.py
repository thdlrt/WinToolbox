import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from toolbox.features import ai_calls, ai_quota
from toolbox.jobs import Jobs
from toolbox.settings import Settings
from toolbox.storage import Storage


@pytest.fixture
def feature(tmp_path, monkeypatch):
    seal = lambda v, decrypt=False: v[7:] if decrypt else 'sealed:'+v
    monkeypatch.setattr(ai_calls, 'protect', seal)
    monkeypatch.setattr(ai_quota, 'protect', seal)
    app = SimpleNamespace(data_dir=tmp_path, storage=Storage(tmp_path), settings=Settings(tmp_path), emit=lambda *a, **kw: None)
    app.jobs = Jobs(app)
    quota = ai_quota.AiQuota(app)
    ids = [quota.save(dict(provider=p, credential='a=b' if p == 'qianwen' else 'synthetic-private-key'))['id']
           for p in ('kimi_code', 'qianwen', 'opencode_go')]
    f = ai_calls.AiCalls(app, quota)
    app.jobs.register('ai_calls.run', f.run_job)
    yield f, ids
    app.jobs.pool.shutdown(wait=True)


def stream(text='OK', finish='stop', usage=None, done=True):
    events = [{'model': 'actual-model', 'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]},
              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}]
    if usage is not None:
        events.append({'choices': [], 'usage': usage})
    raw = ''.join('data: '+json.dumps(e)+'\n\n' for e in events)
    return httpx.Response(200, text=raw+('data: [DONE]\n\n' if done else ''), headers={'Content-Type': 'text/event-stream'})


def wait(f, job):
    deadline = time.monotonic()+5
    while job['status'] in ('queued', 'running', 'cancelling'):
        assert time.monotonic() < deadline
        time.sleep(.01)
        job = f.app.jobs.get(job['id'])
    return job


def test_private_key_save_and_quota_cookie_stays_separate(feature):
    f, ids = feature
    before = f.quota._get(ids[1])
    assert f.listing()['routes'][1]['ready'] is False
    result = f.save({'account_id': ids[1], 'api_key': 'private-plan-key'})
    assert result['ready'] is True
    assert 'private-plan-key' not in json.dumps(f.listing())
    assert 'credential' not in result and 'api_key' not in result
    assert f.quota._get(ids[1]) == before
    assert f.key(f.route(ids[1])) == 'private-plan-key'
    assert 'qianwenaiapi.com' in result['base_url']


def test_three_platform_calls_are_concurrent_and_independent(feature):
    f, ids = feature
    f.save({'account_id': ids[1], 'api_key': 'private-plan-key'})
    barrier = threading.Barrier(3)
    hosts = set()
    def handle(req):
        hosts.add(req.url.host)
        assert req.headers['User-Agent'].startswith('WinToolbox/')
        if req.url.host == 'opencode.ai':
            assert req.headers['x-opencode-session'].startswith('wintoolbox-')
        barrier.wait(timeout=2)
        return stream(usage={'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7})
    f.transport = httpx.MockTransport(handle)
    spec = {'request_id': 'concurrent-fixture', 'tasks': [{'account_id': id, 'prompt': 'Review this synthetic code.'} for id in ids]}
    first = f.submit(spec)
    assert f.submit(spec)['id'] == first['id']
    job = wait(f, first)
    assert job['status'] == 'completed' and job['result']['successful'] == 3
    assert len(hosts) == 3
    assert [r['index'] for r in job['result']['results']] == [0, 1, 2]
    for row in job['result']['results']:
        assert row['model'] == 'actual-model' and row['total_tokens'] == 7
        assert row['tps'] is not None and row['actual_cost'] is None
    assert 'private-plan-key' not in json.dumps(f.get({'request_id': spec['request_id']}))
    assert f.listing()['routes'][0]['usage']['requests'] == 1


@pytest.mark.parametrize('status', [401, 403, 429, 500, 302])
def test_one_failure_does_not_drop_success_or_leak_body(feature, status):
    f, ids = feature
    f.transport = httpx.MockTransport(lambda r: httpx.Response(status, json={'error': 'ECHO_PRIVATE_KEY'}) if r.url.host == 'api.kimi.com' else stream())
    job = wait(f, f.submit({'request_id': 'partial-fixture', 'tasks': [{'account_id': id, 'prompt': 'fixture'} for id in (ids[0], ids[2])]}))
    assert job['status'] == 'completed'
    assert job['result']['successful'] == 1 and job['result']['failed'] == 1
    assert 'ECHO_PRIVATE_KEY' not in json.dumps(job)
    assert job['result']['results'][1]['total_tokens'] is None


@pytest.mark.parametrize('done,finish,status', [(False, 'stop', 'error'), (True, 'length', 'incomplete'), (True, None, 'error')])
def test_incomplete_streams_keep_partial_text(feature, done, finish, status):
    f, ids = feature
    f.transport = httpx.MockTransport(lambda r: stream('partial', finish, done=done))
    job = wait(f, f.submit({'request_id': 'incomplete-fixture', 'tasks': [{'account_id': ids[0], 'prompt': 'fixture'}]}))
    row = job['result']['results'][0]
    assert row['status'] == status and row['text'] == 'partial'
    assert row['total_tokens'] is None and row['actual_cost'] is None


def test_tool_call_deltas_are_returned_without_execution(feature):
    f, ids = feature
    events = [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_1', 'function': {'name': 'inspect', 'arguments': '{'}}]}}]},
              {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"id":1}'}}]}, 'finish_reason': 'tool_calls'}]}]
    f.transport = httpx.MockTransport(lambda r: httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'))
    row = wait(f, f.submit({'request_id': 'tool-fixture', 'tasks': [{'account_id': ids[0], 'prompt': 'fixture', 'tools': []}]}))['result']['results'][0]
    assert row['status'] == 'success'
    assert row['tool_calls'][0]['function'] == {'name': 'inspect', 'arguments': '{"id":1}'}


def test_null_tool_fields_are_empty_deltas_then_arguments_continue(feature):
    f, ids = feature
    events = [
        {'choices': [{'delta': {'reasoning_content': 'partial reasoning'}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_null',
                                              'function': {'name': 'inspect', 'arguments': None}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': None}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': None, 'arguments': None}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '{"ids":['}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': None, 'arguments': '"m_fixture"]}'}}]},
                      'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}},
    ]
    wire = ''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
    f.transport = httpx.MockTransport(lambda _: httpx.Response(200, text=wire))
    row = wait(f, f.submit({'request_id': 'null-tool-fields-fixture',
        'tasks': [{'account_id': ids[2], 'prompt': 'fixture', 'tools': []}]}))['result']['results'][0]
    assert row['status'] == 'success' and row['http_status'] is None
    assert row['reasoning'] == 'partial reasoning' and row['total_tokens'] == 20
    assert row['tool_calls'] == [{'id': 'call_null', 'type': 'function',
                                 'function': {'name': 'inspect', 'arguments': '{"ids":["m_fixture"]}'}}]


@pytest.mark.parametrize('fragment,field,value_type', [
    ({'index': 0, 'function': {'arguments': {'secret': 'DO_NOT_RECORD'}}}, 'tool_calls.function.arguments', 'dict'),
    ({'index': 0, 'function': {'name': 123}}, 'tool_calls.function.name', 'int'),
    ({'index': 0, 'function': 'DO_NOT_RECORD'}, 'tool_calls.function', 'str'),
    ({'index': 'DO_NOT_RECORD', 'function': {}}, 'tool_calls.index', 'str'),
])
def test_invalid_tool_fragment_type_preserves_partial_and_safe_diagnostic(feature, fragment, field, value_type):
    f, ids = feature
    events = [
        {'choices': [{'delta': {'content': 'partial text', 'reasoning_content': 'partial reasoning',
            'tool_calls': [{'index': 0, 'id': 'call_partial', 'function': {'name': 'inspect', 'arguments': '{"ids":'}}]}}]},
        {'choices': [{'delta': {'tool_calls': [fragment]}}]},
    ]
    wire = ''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
    f.transport = httpx.MockTransport(lambda _: httpx.Response(200, text=wire))
    row = wait(f, f.submit({'request_id': 'bad-tool-fragment-fixture',
        'tasks': [{'account_id': ids[2], 'prompt': 'fixture', 'tools': []}]}))['result']['results'][0]
    assert row['status'] == 'error' and row['http_status'] == 502
    assert row['text'] == 'partial text' and row['reasoning'] == 'partial reasoning'
    assert row['tool_calls'][0]['function'] == {'name': 'inspect', 'arguments': '{"ids":'}
    assert row['upstream_error']['error']['code'] == 'invalid_tool_call_fragment'
    assert row['upstream_error']['field'] == field and row['upstream_error']['value_type'] == value_type
    assert row['total_tokens'] is None and 'DO_NOT_RECORD' not in json.dumps(row)


def test_get_exact_proxy_record_excludes_input_and_credentials(feature):
    f, ids = feature
    request_id = 'proxy-'+'a'*32
    f._put('ai_call_results_v1', request_id+':0', {
        'id': request_id+':0', 'request_id': request_id, 'index': 0, 'account_id': ids[2],
        'status': 'error', 'error': 'fixture failure', 'http_status': 502, 'text': 'partial',
        'credential': 'DO_NOT_RECORD', 'prompt': 'DO_NOT_RECORD', 'messages': ['DO_NOT_RECORD'],
        'spec': {'api_key': 'DO_NOT_RECORD'},
    }, request_id)
    result = f.get({'request_id': request_id})
    assert result['source'] == 'proxy' and result['job_id'] is None and result['job'] is None
    assert result['results'][0]['text'] == 'partial'
    assert 'DO_NOT_RECORD' not in json.dumps(result)
    for invalid in [request_id+':0', 'proxy-'+'a'*31, 'proxy-', 'proxy-'+'.'*32]:
        with pytest.raises(ValueError, match='批次不存在'):
            f.get({'request_id': invalid})


def test_no_silent_retry_and_id_conflicts(feature):
    f, ids = feature
    calls = []
    f.transport = httpx.MockTransport(lambda r: calls.append(r) or stream())
    spec = {'request_id': 'no-retry-fixture', 'tasks': [{'account_id': ids[0], 'prompt': 'fixture'}]}
    job = wait(f, f.submit(spec))
    assert f.submit(spec)['id'] == job['id'] and len(calls) == 1
    with pytest.raises(ValueError, match='其他参数'):
        f.submit({**spec, 'concurrency': 1})
    retried = wait(f, f.app.jobs.retry(job['id']))
    assert retried['status'] == 'failed' and len(calls) == 1


def test_disable_and_reject_secret_or_endpoint_in_job_params(feature):
    f, ids = feature
    f.save({'account_id': ids[0], 'enabled': False})
    with pytest.raises(ValueError, match='停用'):
        f.submit({'request_id': 'disabled-fixture', 'tasks': [{'account_id': ids[0], 'prompt': 'fixture'}]})
    for extra in ({'api_key': 'DO_NOT_SAVE'}, {'base_url': 'https://evil.example'}):
        with pytest.raises(ValueError, match='未知字段'):
            f.submit({'request_id': 'invalid-fixture', 'tasks': [{'account_id': ids[2], 'prompt': 'fixture', **extra}]})


def test_job_direct_submission_rejects_credentials(feature):
    f, _ = feature
    with pytest.raises(ValueError, match='凭据'):
        f.app.jobs.submit('ai_calls.run', {'request_id': 'fixture', 'api_key': 'PRIVATE'})
    assert f.app.jobs.list() == []


def test_remove_account_removes_call_key(feature):
    f, ids = feature
    f.save({'account_id': ids[1], 'api_key': 'synthetic-private-key'})
    f.quota.remove({'id': ids[1]})
    assert f._get('ai_call_routes_v1', ids[1]) is None


def test_qianwen_api_only_account_does_not_require_console_cookie(feature):
    f, _ = feature
    row = f.quota.save({'provider': 'qianwen', 'api_only': True})
    f.save({'account_id': row['id'], 'api_key': 'fixture-api-only-key'})
    assert f.key(f.route(row['id'])) == 'fixture-api-only-key'


def test_cancel_prevents_queued_calls_and_keeps_receipts(feature):
    f, ids = feature
    entered, release = threading.Event(), threading.Event()
    calls = []
    def handle(r):
        calls.append(r)
        entered.set()
        release.wait(2)
        return stream()
    f.transport = httpx.MockTransport(handle)
    job = f.submit({'request_id': 'cancel-fixture', 'concurrency': 1, 'tasks': [{'account_id': id, 'prompt': 'fixture'} for id in (ids[0], ids[2])]})
    assert entered.wait(2)
    f.app.jobs.cancel(job['id'])
    release.set()
    job = wait(f, job)
    assert job['status'] == 'cancelled' and len(calls) == 1
    results = f.get({'request_id': 'cancel-fixture'})['results']
    assert len(results) == 2 and all(r['status'] == 'cancelled' for r in results)
