import http.client
import json

import httpx
import pytest

from test_ai_proxy import proxy, feature
from test_ai_calls import stream
from toolbox.features.ai_proxy import MAX_BODY
from toolbox.features.ai_calls import MAX_ERROR_BODY


def test_four_mib_image_request_reaches_only_mock_upstream(proxy):
    p, f, ids, token, api = proxy
    calls = []
    image = 'data:image/png;base64,' + 'A' * (4 * 1024 * 1024)
    def upstream(request):
        payload = json.loads(request.content)
        calls.append(len(request.content))
        assert payload['messages'][0]['content'][0]['image_url']['url'] == image
        return stream('mock success')
    f.transport = httpx.MockTransport(upstream)
    response = api.post('/chat/completions', json={
        'model': 'opencode_go/deepseek-v4.1-flash',
        'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': image}}]}],
    })
    assert response.status_code == 200 and len(calls) == 1
    assert calls[0] > 4 * 1024 * 1024
    assert p.status()['max_request_bytes'] == MAX_BODY == 16 * 1024 * 1024
    assert response.headers['x-wintoolbox-request-id'].startswith('proxy-')


def test_over_sixteen_mib_is_rejected_before_body_or_upstream(proxy):
    p, f, ids, token, api = proxy
    f.transport = httpx.MockTransport(lambda _: pytest.fail('oversized input reached upstream'))
    connection = http.client.HTTPConnection('127.0.0.1', p.config['port'], timeout=3)
    connection.putrequest('POST', '/v1/chat/completions')
    connection.putheader('Authorization', 'Bearer ' + token['api_key'])
    connection.putheader('Content-Type', 'application/json')
    connection.putheader('Content-Length', str(MAX_BODY + 1))
    connection.endheaders()  # No large body is sent or buffered in this guard test.
    response = connection.getresponse()
    assert response.status == 413 and '16 MiB' in response.read().decode()
    connection.close()


def test_structured_400_diagnostics_are_sanitized_and_correlated(proxy):
    p, f, ids, token, api = proxy
    message = ('server_error: transport_retryable_error; key=synthetic-private-key; '
               'Authorization: Bearer other-private; oc_sk_abc123; ' + token['api_key'] +
               '; data:image/png;base64,' + 'A' * 180)
    f.transport = httpx.MockTransport(lambda _: httpx.Response(400,
        json={'error': {'type': 'server_error', 'code': 'transport_retryable_error', 'message': message,
                        'request_payload': 'DO_NOT_RETAIN_THIS'}},
        headers={'x-request-id': 'upstream-request-123', 'Authorization': 'DO_NOT_RETAIN_HEADER'}))
    response = api.post('/chat/completions', json={'model': 'opencode_go/deepseek-v4.1-flash',
                                                'messages': [{'role': 'user', 'content': 'fixture'}]})
    assert response.status_code == 400
    error = response.json()['error']
    assert error['request_id'] == response.headers['x-wintoolbox-request-id']
    assert error['upstream']['error']['code'] == 'transport_retryable_error'
    assert error['upstream']['request_ids'] == {'x-request-id': 'upstream-request-123'}
    stored = f._get('ai_call_results_v1', error['request_id'] + ':0')
    assert stored['upstream_error'] == error['upstream'] and stored['http_status'] == 400
    retrieved = f.get({'request_id': error['request_id']})
    assert retrieved['source'] == 'proxy' and retrieved['results'][0] == stored
    text = json.dumps([error, stored])
    for secret in ['synthetic-private-key', 'other-private', 'oc_sk_abc123', token['api_key'],
                   'A' * 180, 'DO_NOT_RETAIN_THIS', 'DO_NOT_RETAIN_HEADER']:
        assert secret not in text
    assert 'transport_retryable_error' in text


@pytest.mark.parametrize('raw,expected', [
    (b'PRIVATE_HTML_BODY', 'non_json_not_retained'),
    (b'', 'empty'),
    (b'A' * (MAX_ERROR_BODY + 500), 'truncated_not_retained'),
    (json.dumps({'error': {'message': '{"messages":[{"role":"user","content":"PRIVATE_PROMPT"}]}'}}).encode(), 'structured_fields_only'),
])
def test_arbitrary_or_request_echo_bodies_are_not_retained(proxy, raw, expected):
    p, f, ids, token, api = proxy
    f.transport = httpx.MockTransport(lambda _: httpx.Response(400, content=raw))
    response = api.post('/chat/completions', json={'model': 'opencode_go/deepseek-v4.1-flash',
                                                'messages': [{'role': 'user', 'content': 'fixture'}]})
    diagnostic = response.json()['error']['upstream']
    assert diagnostic['body_status'] == expected
    assert 'PRIVATE_HTML_BODY' not in response.text and 'PRIVATE_PROMPT' not in response.text
    assert 'A' * 160 not in response.text
    assert diagnostic['body_bytes_read'] <= MAX_ERROR_BODY + 1


def test_stream_error_after_content_keeps_safe_detail_and_request_id(proxy):
    p, f, ids, token, api = proxy
    events = [
        {'choices': [{'index': 0, 'delta': {'content': 'partial'}}]},
        {'error': {'type': 'server_error', 'code': 'stream_failed',
                   'message': 'stream_failed synthetic-private-key Bearer secret-other'}},
    ]
    wire = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
    f.transport = httpx.MockTransport(lambda _: httpx.Response(200, text=wire,
        headers={'Content-Type': 'text/event-stream', 'x-request-id': 'stream-request-456'}))
    response = api.post('/chat/completions', json={'model': 'opencode_go/deepseek-v4.1-flash',
        'messages': [{'role': 'user', 'content': 'fixture'}], 'stream': True})
    assert response.status_code == 200
    parsed = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith('data: ') and line != 'data: [DONE]']
    error = next(e['error'] for e in parsed if 'error' in e)
    assert error['request_id'] == response.headers['x-wintoolbox-request-id']
    assert error['upstream']['phase'] == 'stream'
    assert error['upstream']['error']['code'] == 'stream_failed'
    assert 'synthetic-private-key' not in response.text and 'secret-other' not in response.text
    row = f._get('ai_call_results_v1', error['request_id'] + ':0')
    assert row['text'] == 'partial' and row['status'] == 'error'


def test_error_body_read_failure_does_not_erase_http_status(proxy):
    p, f, ids, token, api = proxy
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadError('secret-in-exception-must-not-be-retained')
            yield b''
    f.transport = httpx.MockTransport(lambda _: httpx.Response(400, stream=Broken()))
    response = api.post('/chat/completions', json={'model': 'opencode_go/deepseek-v4.1-flash',
        'messages': [{'role': 'user', 'content': 'fixture'}]})
    assert response.status_code == 400
    assert response.json()['error']['upstream']['body_status'] == 'read_failed'
    assert 'secret-in-exception' not in response.text


def test_null_tool_deltas_keep_proxy_stream_and_final_record(proxy):
    p, f, ids, token, api = proxy
    events = [
        {'choices': [{'delta': {'reasoning_content': 'partial reasoning'}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_null',
                                              'function': {'name': 'inspect', 'arguments': None}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': None}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': None, 'arguments': '{"ids":'}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '[]}'}}]},
                      'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 6, 'total_tokens': 16}},
    ]
    wire = ''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
    f.transport = httpx.MockTransport(lambda _: httpx.Response(200, text=wire))
    response = api.post('/chat/completions', json={'model': 'opencode_go/deepseek-v4.1-flash',
        'messages': [{'role': 'user', 'content': 'fixture'}], 'stream': True})
    assert response.status_code == 200 and 'data: [DONE]' in response.text
    parsed = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith('data: ') and line != 'data: [DONE]']
    assert not any('error' in event for event in parsed)
    row = f.get({'request_id': response.headers['x-wintoolbox-request-id']})['results'][0]
    assert row['status'] == 'success' and row['total_tokens'] == 16
    assert row['tool_calls'][0]['function'] == {'name': 'inspect', 'arguments': '{"ids":[]}'}
    assert row['reasoning'] == 'partial reasoning'
