"""Resident RPC/CLI tests use temporary data, fixture credentials and receipts."""
import http.client
import json
from pathlib import Path
import uuid

import pytest

from toolbox import cli
from toolbox.app import App
from toolbox.local_rpc import ENDPOINT_FILE, LocalRpcServer, MAX_REQUEST


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', register_live=False)
    instance.data_sync.stop_worker()
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


@pytest.fixture
def server(app):
    value = LocalRpcServer(app, seal=lambda token: 'fixture-sealed')
    value.start()
    yield value
    value.close()


def client(server):
    return cli.Client(server.app.data_dir, unseal=lambda sealed: server.token)


def post(server, *, auth=True, body=None, headers=None, path='/rpc'):
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_port, timeout=3)
    values = {'Content-Type': 'application/json'}
    if auth:
        values['Authorization'] = 'Bearer ' + server.token
    values.update(headers or {})
    body = json.dumps(body if body is not None else {'jsonrpc': '2.0', 'id': 1, 'method': 'expenses.projects.list'})
    try:
        connection.request('POST', path, body=body, headers=values)
        result = connection.getresponse()
        return result.status, json.loads(result.read())
    finally:
        connection.close()


def test_loopback_endpoint_seals_token_and_cleans_up(server):
    path = Path(server.app.data_dir) / ENDPOINT_FILE
    config = json.loads(path.read_text('utf-8'))
    assert config['host'] == '127.0.0.1' and server.server.server_address[0] == '127.0.0.1'
    assert config['token_dpapi'] == 'fixture-sealed' and server.token not in path.read_text('utf-8')
    assert client(server).call('app.info')['data_dir'] == str(server.app.data_dir)
    server.close()
    assert not path.exists()
    server.close()


@pytest.mark.parametrize('headers,path,expected', [
    ({'Origin': 'http://127.0.0.1'}, '/rpc', 403),
    ({'Host': 'foreign.example'}, '/rpc', 403),
    ({'Content-Type': 'text/plain'}, '/rpc', 415),
    ({'Content-Length': str(MAX_REQUEST + 1)}, '/rpc', 413),
    ({}, '/other', 403),
    ({'Authorization': 'Bearer wrong'}, '/rpc', 401),
])
def test_unauthorized_or_browser_requests_cannot_mutate(server, headers, path, expected):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'expenses.save',
            'params': {'title': 'must not exist', 'date': '2026-10-25', 'amount': '1.00'}}
    assert post(server, body=body, headers=headers, path=path)[0] == expected
    assert server.app.call('expenses.list', {'month': '2026-10'})['items'] == []
    assert post(server, auth=False)[0] == 401


@pytest.mark.parametrize('body', [[], {}, {'jsonrpc': '2.0', 'id': 1, 'method': 'expenses.list', 'params': []}])
def test_invalid_rpc_rejected_before_dispatch(server, body):
    status, result = post(server, body=body)
    assert status == 200 and result['error']['code'] == -32600


def test_method_discovery_and_native_lifecycle_restriction(server):
    api = client(server)
    methods = api.call('cli.methods', {'prefix': 'expenses.'})['methods']
    assert 'expenses.commit' in methods and 'expenses.projects.list' in methods
    assert all(name.startswith('expenses.') for name in methods)
    with pytest.raises(cli.CliError, match='桌面程序管理'):
        api.call('app.prepare_exit')
    with pytest.raises(cli.CliError, match='方法不存在'):
        api.call('missing.operation')


def test_cli_atomic_personal_reimbursement_with_receipt_and_idempotent_retry(server, tmp_path, capsys):
    receipt = tmp_path / 'fixture.pdf'
    receipt.write_bytes(b'%PDF-1.4\nFixture receipt\n%%EOF')
    data = {'title': 'Kimi fixture', 'date': '2026-10-25', 'amount': '199.00',
            'currency': 'CNY', 'split_enabled': False, 'status': 'waiting',
            'request_id': str(uuid.uuid4()), 'attachments_add': [{'path': str(receipt), 'kind': 'invoice'}]}
    source = tmp_path / 'entry.json'
    source.write_text(json.dumps(data), 'utf-8')
    args = cli.parser().parse_args(['expenses', 'save', '--input', str(source)])
    args.wait_timeout = 5
    first = cli.run(args, client(server))
    second = cli.run(args, client(server))
    assert first['status'] == second['status'] == 'completed'
    entry = first['result']['entry']
    assert entry['id'] == second['result']['entry']['id']
    rows = client(server).call('expenses.list', {'month': '2026-10'})
    assert len(rows['items']) == 1
    assert rows['summary'][0]['pending'] == '199.00' and rows['summary'][0]['share_due'] == '0.00'
    assert rows['items'][0]['split_enabled'] is False and len(rows['items'][0]['attachments']) == 1
    attached = client(server).call('expenses.attachment', {'id': entry['id'], 'attachment_id': entry['attachments'][0]['id']})
    assert Path(attached['path']).read_bytes() == receipt.read_bytes()
    assert 'request_id=' + data['request_id'] in capsys.readouterr().err


def test_cli_returns_json_and_nonzero_for_rpc_or_job_error(server, monkeypatch, capsys):
    api = client(server)
    monkeypatch.setattr(cli, 'Client', lambda *a, **kw: api)
    assert cli.main(['expenses', 'projects']) == 0
    assert json.loads(capsys.readouterr().out)['items'][0]['name'] == 'AI报销'
    assert cli.main(['call', 'missing.method']) == 1
    output = capsys.readouterr()
    assert output.out == '' and '方法不存在' in json.loads(output.err)['error']
    with pytest.raises(cli.CliError, match='failed'):
        cli.wait_job(api, {'id': 'fixture', 'status': 'failed', 'error': 'fixture error'}, 1)


def test_client_rejects_remote_endpoint_without_unsealing_or_connecting(tmp_path):
    (tmp_path / ENDPOINT_FILE).write_text(json.dumps({'protocol': 1, 'host': 'remote.example', 'port': 80}), 'utf-8')
    def never(_):
        raise AssertionError('must not unseal foreign endpoint')
    with pytest.raises(cli.CliError, match='凭据'):
        cli.Client(tmp_path, unseal=never)
    with pytest.raises(cli.CliError, match='尚未运行'):
        cli.Client(tmp_path / 'missing', unseal=never)


def test_ai_cli_dispatches_batch_without_changing_request_id(tmp_path):
    calls = []
    api = type('FixtureClient', (), {'call': lambda self, method, params: calls.append((method, params)) or {'ok': True}})()
    spec = {'request_id': 'stable-development-request', 'tasks': [{'account_id': 'fixture', 'prompt': 'test'}]}
    path = tmp_path / 'batch.json'
    path.write_text(json.dumps(spec), 'utf-8')
    cli.run(cli.parser().parse_args(['ai', 'submit', '--input', str(path), '--no-wait']), api)
    cli.run(cli.parser().parse_args(['ai', 'list']), api)
    cli.run(cli.parser().parse_args(['ai', 'get', spec['request_id']]), api)
    assert calls == [('ai_calls.submit', spec), ('ai_calls.list', {}), ('ai_calls.get', {'request_id': spec['request_id']})]


def test_old_server_cannot_remove_new_instance_endpoint(app):
    first = LocalRpcServer(app, seal=lambda _: 'fixture')
    second = LocalRpcServer(app, seal=lambda _: 'fixture')
    try:
        first.start()
        second.start()
        first.close()
        assert json.loads(second.path.read_text('utf-8'))['instance'] == second.instance
    finally:
        first.close()
        second.close()
