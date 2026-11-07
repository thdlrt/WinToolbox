import concurrent.futures
import json
import socket
import threading

import httpx
import pytest

from test_ai_calls import feature, stream
from toolbox.features.ai_proxy import AiProxy


@pytest.fixture
def proxy(feature):
    f, ids = feature
    f.app.handlers = {}
    f.app.register = lambda name, method: f.app.handlers.update({name: method})
    p = AiProxy(f.app, f)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    p.configure({'port': port})
    token = p.create_key({'name': 'fixture application'})
    api = httpx.Client(base_url=token['base_url'], headers={'Authorization': 'Bearer '+token['api_key']}, trust_env=False)
    f.transport = httpx.MockTransport(lambda req: stream(usage={'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}))
    yield p, f, ids, token, api
    api.close()
    p.close()


def test_keys_are_hashed_only_and_revocation_blocks_requests(proxy):
    p, f, ids, token, api = proxy
    public = json.dumps(p.status())
    assert token['api_key'] not in public and 'digest' not in public
    assert token['api_key'] not in json.dumps(p.keys())
    assert api.get('/models').status_code == 200
    p.revoke_key({'id': token['id']})
    assert api.get('/models').status_code == 401
    assert p.status()['keys'][0]['revoked'] is True


def test_loopback_only_auth_origin_and_host_guards(proxy):
    p, f, ids, token, api = proxy
    assert p.server.server_address[0] == '127.0.0.1'
    assert api.get('/models', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert api.get('/models', headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert api.get('/models', headers={'Host': 'untrusted.example'}).status_code == 403
    assert api.get('/responses').status_code == 404


def test_model_list_namespaces_and_caches_real_models(proxy):
    p, f, ids, token, api = proxy
    queries = []
    f.transport = httpx.MockTransport(lambda r: queries.append(r) or httpx.Response(200, json={'data': [{'id': 'fixture-model'}, {'id': 'wan-video'}, {'id': 'qwen-image-3.0'}]}))
    models = api.get('/models').json()['data']
    assert {r['id'] for r in models} == {'kimi_code/fixture-model', 'opencode_go/fixture-model'}
    assert len(queries) == 2
    assert api.get('/models').json()['data'] == models and len(queries) == 2


def test_completion_and_upstream_options_return_standard_json(proxy):
    p, f, ids, token, api = proxy
    requests = []
    f.transport = httpx.MockTransport(lambda r: requests.append(json.loads(r.content)) or stream('fixture answer', usage={'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}))
    response = api.post('/chat/completions', json={'model': 'kimi_code/kimi-for-coding', 'messages': [{'role': 'user', 'content': 'fixture'}], 'temperature': .2, 'max_tokens': 100})
    assert response.status_code == 200
    body = response.json()
    assert body['object'] == 'chat.completion' and body['model'] == 'kimi_code/kimi-for-coding'
    assert body['choices'][0]['message']['content'] == 'fixture answer' and body['usage']['total_tokens'] == 6
    assert requests[0]['model'] == 'kimi-for-coding' and requests[0]['temperature'] == .2
    assert f.listing()['routes'][0]['usage']['requests'] == 1


def test_stream_is_forwarded_before_upstream_finishes(proxy):
    p, f, ids, token, api = proxy
    first_read, release = threading.Event(), threading.Event()
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"first"}}]}\n\n'
            release.wait(2)
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    f.transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Chunks()))
    def read():
        with api.stream('POST', '/chat/completions', json={'model': 'kimi_code/kimi-for-coding', 'messages': [{'role': 'user', 'content': 'fixture'}], 'stream': True}) as response:
            assert response.status_code == 200
            lines = []
            for line in response.iter_lines():
                lines.append(line)
                if 'first' in line:
                    first_read.set()
                    release.set()
            return lines
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(read)
        assert first_read.wait(1), 'gateway buffered the entire upstream response'
        assert 'data: [DONE]' in future.result(timeout=3)


@pytest.mark.parametrize('code', [401, 403, 429, 500])
def test_upstream_errors_preserve_status_without_body_leak(proxy, code):
    p, f, ids, token, api = proxy
    f.transport = httpx.MockTransport(lambda r: httpx.Response(code, json={'error': 'PRIVATE_UPSTREAM_ECHO'}))
    response = api.post('/chat/completions', json={'model': 'kimi_code/kimi-for-coding', 'messages': [{'role': 'user', 'content': 'fixture'}]})
    assert response.status_code == code
    assert 'PRIVATE_UPSTREAM_ECHO' not in response.text


def test_three_platform_requests_really_run_concurrently(proxy):
    p, f, ids, token, api = proxy
    f.save({'account_id': ids[1], 'api_key': 'fixture-plan-key'})
    barrier = threading.Barrier(3)
    f.transport = httpx.MockTransport(lambda r: barrier.wait(2) is not None and stream())
    names = ['kimi_code/kimi-for-coding', 'qianwen/qwen3.8-flash', 'opencode_go/deepseek-v4.1-flash']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda model: api.post('/chat/completions', json={'model': model, 'messages': [{'role': 'user', 'content': 'fixture'}]}), names))
    assert [r.status_code for r in responses] == [200, 200, 200]


@pytest.mark.parametrize('payload', [None, {}, {'model': 'unknown'}, {'model': 'kimi_code/kimi-for-coding', 'messages': [], 'api_key': 'PRIVATE'}])
def test_invalid_requests_do_not_reach_model(proxy, payload):
    p, f, ids, token, api = proxy
    f.transport = httpx.MockTransport(lambda r: pytest.fail('invalid request reached provider'))
    response = api.post('/chat/completions', content=json.dumps(payload), headers={'Content-Type': 'application/json'})
    assert response.status_code == 400


def test_key_survives_restart_without_plaintext_copy(proxy):
    p, f, ids, token, api = proxy
    p.close()
    restarted = AiProxy(f.app, f)
    assert restarted.authorized('Bearer '+token['api_key'])
    assert token['api_key'] not in json.dumps(restarted.status())
    assert restarted.config['port'] == p.config['port']


def test_one_click_fetches_full_catalogue_and_persists_it(proxy):
    p, f, ids, token, api = proxy
    f.save({'account_id': ids[1], 'api_key': 'fixture-plan-key'})
    requests = []
    def models(req):
        requests.append(req)
        assert req.method == 'GET'
        return httpx.Response(200, json={'data': [{'id': 'model-a'}, {'id': 'model-b'}]})
    f.transport = httpx.MockTransport(models)
    job = p.submit_model_refresh({})
    from test_ai_calls import wait
    done = wait(f, job)
    assert done['status'] == 'completed'
    assert len(done['result']['models']) == 6 and len(requests) == 3
    assert done['result']['models_refreshed_at'] is not None
    p.close()
    restarted = AiProxy(f.app, f)
    assert len(restarted.status()['models']) == 6
    assert len(requests) == 3  # Reading UI state never calls a provider.


def test_catalogue_failure_keeps_cached_models_and_reports_error(proxy):
    p, f, ids, token, api = proxy
    f.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'id': 'cached-model'}]}))
    p.refresh_catalogue(force=True)
    before = p.status()['models']
    f.transport = httpx.MockTransport(lambda r: httpx.Response(401, json={'error': 'PRIVATE'}))
    p.refresh_catalogue(force=True)
    after = p.status()
    assert after['models'] == before
    assert len(after['model_errors']) == 2 and 'PRIVATE' not in json.dumps(after)
    f.save({'account_id': ids[0], 'enabled': False})
    assert all(not m['id'].startswith('kimi_code/') for m in p.status()['models'])
