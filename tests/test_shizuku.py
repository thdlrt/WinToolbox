"""Synthetic ADB protocols: no phones, real pairing codes or network scanning."""
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolbox.app import App
from toolbox.features import shizuku
from toolbox.jobs import Cancelled


PHONE = '192.168.1.20:37123'
PAIR = '192.168.1.20:42123'


class FakeADB:
    def __init__(self):
        self.calls = []
        self.mdns = f'List of discovered mdns services\nadb-phone _adb-tls-connect._tcp {PHONE} phone.local\nadb-pair _adb-tls-pairing._tcp {PAIR}\n'
        self.devices = f'List of devices attached\n{PHONE} device model:Pixel_8 transport_id:1\nUSB_SERIAL device model:Other_phone\n'
        self.running = False
        self.installed = True
        self.connect_ok = True
        self.script_ok = True
        self.script_starts = True
        self.pair_ok = True
        self.mdns_failed = False
        self.fail_mdns_after_pair = False
        self.paired = False

    def __call__(self, job, args, **kwargs):
        job.check_cancelled()
        self.calls.append((list(args), dict(kwargs)))
        if args == ['mdns', 'services']:
            if self.fail_mdns_after_pair and self.paired:
                raise TimeoutError('Discovery timed out')
            return (1, 'mDNS unavailable') if self.mdns_failed else (0, self.mdns)
        if args == ['devices', '-l']:
            return 0, self.devices
        if args[0] == 'pair':
            assert args == ['pair', PAIR]
            assert kwargs['input_text'] == '123456'
            self.paired = self.pair_ok
            return (0, f'Successfully paired to {PAIR}') if self.pair_ok else (1, 'failed pairing with code 123456')
        if args[0] == 'connect':
            return (0, 'connected to ' + args[1]) if self.connect_ok else (0, 'failed to connect: Connection refused')
        assert args[:2] == ['-s', PHONE], 'Every device command must explicitly target this phone'
        command = args[2:]
        if command == ['get-state']:
            return 0, 'device'
        if command == ['shell', 'pm', 'path', shizuku.PACKAGE]:
            return (0, 'package:/data/app/Shizuku/base.apk') if self.installed else (1, '')
        if command == ['shell', 'pidof', 'shizuku_server']:
            return (0, '2314') if self.running else (1, '')
        if command == ['shell', 'sh', shizuku.START_SCRIPT]:
            self.running = self.script_starts and self.script_ok
            return (0, 'Shizuku started successfully') if self.script_ok else (1, 'start.sh missing')
        raise AssertionError('Unexpected ADB command: ' + repr(args))


@pytest.fixture
def app(tmp_path, monkeypatch):
    instance = App(tmp_path / 'data', register_live=False)
    instance.fake_adb = FakeADB()
    monkeypatch.setattr(shizuku, 'run_adb', instance.fake_adb)
    monkeypatch.setattr(shizuku, 'adb_path', lambda: str(tmp_path / 'adb.exe'))
    monkeypatch.setattr(shizuku, 'POLL_SECONDS', .001)
    monkeypatch.setattr(shizuku, 'POLL_ATTEMPTS', 2)
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def finish(app, job, expected='completed'):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        row = app.jobs.get(job['id'])
        if row['status'] not in ('queued', 'running', 'cancelling') and job['id'] not in app.jobs.active:
            assert row['status'] == expected, row
            return row if expected != 'completed' else row['result']
        time.sleep(.01)
    raise AssertionError('Synthetic ADB job timed out')


@pytest.mark.parametrize('value', ['8.8.8.8:1234', '127.0.0.1:1234', '169.254.1.2:1234', '224.1.2.3:1234',
                                   '192.168.1.255:1234', '192.168.1.0:1234', '192.168.01.2:1234',
                                   '192.168.1.2:0', '192.168.1.2:65536', 'phone.local:1234',
                                   'https://192.168.1.2:1234', '192.168.1.2:1234;echo hacked', None])
def test_address_rejects_nonprivate_and_command_injection(value):
    with pytest.raises(ValueError):
        shizuku.target_address(value)


def test_valid_private_addresses_and_adb_resolution_order(tmp_path, monkeypatch):
    for value in ('10.1.2.3:1', '172.16.1.2:65535', '172.31.9.8:12345', PHONE):
        assert shizuku.target_address(value) == value
    explicit = tmp_path / 'explicit.exe'
    explicit.write_bytes(b'fixture')
    tools = tmp_path / 'tools'
    bundled = tools / 'adb' / ('adb.exe' if os.name == 'nt' else 'adb')
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b'fixture')
    monkeypatch.setenv('WINTOOLBOX_ADB', str(explicit))
    monkeypatch.setenv('WINTOOLBOX_TOOLS', str(tools))
    monkeypatch.setattr(shizuku.shutil, 'which', lambda _: 'PATH-ADB')
    assert shizuku.adb_path() == str(explicit.resolve())
    explicit.unlink()
    assert shizuku.adb_path() == str(bundled.resolve())


def test_discovery_uses_only_mdns_and_devices_and_never_scans_network(app):
    result = finish(app, app.call('shizuku.discover'))
    assert [call[0] for call in app.fake_adb.calls] == [['mdns', 'services'], ['devices', '-l']]
    assert {row['address'] for row in result['devices']} == {PHONE, PAIR}
    connected = next(row for row in result['devices'] if row['kind'] == 'connect')
    assert connected['state'] == 'device' and connected['label'] == 'Pixel 8'
    app.fake_adb.mdns_failed = True
    fallback = finish(app, app.call('shizuku.discover'))
    assert fallback['devices'][0]['address'] == PHONE and 'mDNS' in fallback['notice']


def test_mdns_serials_merge_to_same_tls_device_and_legacy_adb_service_ignored():
    mdns = f'adb-phone _adb-tls-connect._tcp {PHONE}\nlegacy _adb._tcp 192.168.1.30:5555\nexternal _adb-tls-connect._tcp 8.8.8.8:1234'
    devices = 'adb-phone._adb-tls-connect._tcp device model:Pixel\nUSB_SERIAL device'
    rows = shizuku.parse_discovery(mdns, devices)
    assert len(rows) == 1 and rows[0]['address'] == PHONE and rows[0]['state'] == 'device'


def test_start_runs_official_script_and_confirms_pid_before_success(app):
    app.settings.update({'preferences': {'keep': 'unchanged'}})
    result = finish(app, app.call('shizuku.start', {'target': PHONE}))
    assert result['running'] is True and result['target'] == PHONE
    calls = [row[0] for row in app.fake_adb.calls]
    assert ['-s', PHONE, 'shell', 'sh', shizuku.START_SCRIPT] in calls
    assert calls[-1] == ['-s', PHONE, 'shell', 'pidof', 'shizuku_server']
    assert not any('kill-server' in row or 'tcpip' in row or '5555' in row for row in calls)
    status = app.call('shizuku.status')
    assert status['adb_available'] and status['last_target'] == PHONE
    assert app.settings.get()['preferences']['keep'] == 'unchanged'


def test_existing_shizuku_is_confirmed_without_restarting(app):
    app.fake_adb.running = True
    assert finish(app, app.call('shizuku.start', {'target': PHONE}))['running']
    assert not any(row[0][-1] == shizuku.START_SCRIPT for row in app.fake_adb.calls)


def test_script_success_text_without_process_is_not_reported_as_running(app):
    app.fake_adb.script_starts = False
    result = finish(app, app.call('shizuku.start', {'target': PHONE}))
    assert result['running'] is False and '未检测到' in result['message']


def test_connection_failure_and_missing_app_have_actionable_errors(app):
    app.fake_adb.connect_ok = False
    row = finish(app, app.call('shizuku.start', {'target': PHONE}), 'failed')
    assert '连接端口' in row['error']
    assert 'last_target' not in app.call('shizuku.status')
    app.fake_adb.connect_ok = True
    app.fake_adb.installed = False
    row = finish(app, app.call('shizuku.start', {'target': PHONE}), 'failed')
    assert '未安装 Shizuku' in row['error']
    assert app.call('shizuku.status')['last_target'] == PHONE


def test_missing_adb_and_untrusted_get_state_do_not_claim_connection(app, monkeypatch):
    monkeypatch.setattr(shizuku, 'adb_path', lambda: None)
    assert app.call('shizuku.status') == {'adb_available': False}
    original = app.fake_adb
    def unauthorized(job, args, **kwargs):
        if args == ['-s', PHONE, 'get-state']:
            return 1, 'error: device unauthorized'
        return original(job, args, **kwargs)
    monkeypatch.setattr(shizuku, 'run_adb', unauthorized)
    row = finish(app, app.call('shizuku.start', {'target': PHONE}), 'failed')
    assert '授权' in row['error'] and 'last_target' not in app.call('shizuku.status')
    assert not any('shell' in args for args, _ in app.fake_adb.calls)


def test_pair_stdin_secret_and_unique_same_ip_connect_inference(app):
    result = finish(app, app.call('shizuku.pair', {'pair_target': PAIR, 'code': '123456'}))
    assert result['paired'] and result['running'] and result['target'] == PHONE
    assert app.fake_adb.calls[0] == (['pair', PAIR], {'input_text': '123456', 'timeout': 35})
    records = app.jobs.list()
    assert '123456' not in json.dumps(records)
    assert all('code' not in record['params'] for record in records)
    assert not list((app.data_dir / 'jobs').rglob('*.log'))
    retry = finish(app, app.jobs.retry(records[0]['id']), 'failed')
    assert '重新输入' in retry['error']


@pytest.mark.parametrize('mdns', [f'adb-other _adb-tls-connect._tcp 192.168.1.99:33333',
                                  f'one _adb-tls-connect._tcp {PHONE}\ntwo _adb-tls-connect._tcp 192.168.1.20:33333'])
def test_pair_does_not_guess_missing_or_ambiguous_connect_port(app, mdns):
    app.fake_adb.mdns = mdns
    result = finish(app, app.call('shizuku.pair', {'pair_target': PAIR, 'code': '123456'}))
    assert result['paired'] and not result['running'] and '连接端口' in result['message']
    assert not any(row[0][0] == 'connect' for row in app.fake_adb.calls)


def test_pair_success_survives_later_discovery_or_start_failure(app):
    app.fake_adb.fail_mdns_after_pair = True
    result = finish(app, app.call('shizuku.pair', {'pair_target': PAIR, 'code': '123456'}))
    assert result['paired'] and not result['running'] and '配对成功' in result['message']


def test_explicit_pair_connection_does_not_select_another_discovered_phone(app):
    app.fake_adb.mdns = 'different _adb-tls-connect._tcp 192.168.1.99:40000'
    result = finish(app, app.call('shizuku.pair', {'pair_target': PAIR, 'code': '123456', 'target': PHONE}))
    assert result['running'] and result['target'] == PHONE
    assert not any(args == ['mdns', 'services'] for args, _ in app.fake_adb.calls)
    app.fake_adb.connect_ok = False
    result = finish(app, app.call('shizuku.pair', {'pair_target': PAIR, 'code': '123456', 'target': PHONE}))
    assert result['paired'] and not result['running'] and '配对成功' in result['message']


def test_pair_validates_separate_ports_and_redacts_failure(app):
    for params in ({'target': PAIR}, {'target': '192.168.1.21:37123'}, {'code': '１２３４５６'}, {'code': '12345'}):
        with pytest.raises(ValueError):
            app.call('shizuku.pair', {'pair_target': PAIR, 'code': '123456', **params})
    assert not app.jobs.list()
    app.fake_adb.pair_ok = False
    row = finish(app, app.call('shizuku.pair', {'pair_target': PAIR, 'code': '123456'}), 'failed')
    assert '123456' not in json.dumps(row) and '配对失败' in row['error']


def test_generic_jobs_api_cannot_persist_pairing_code(app):
    for key in ('code', 'pairing_code', 'password'):
        with pytest.raises(ValueError):
            app.call('jobs.submit', {'tool': 'shizuku.pair', 'params': {'pair_target': PAIR, key: '123456'}})
    assert app.jobs.list() == []


def test_adb_runner_code_is_stdin_not_argv_and_output_is_redacted(tmp_path, monkeypatch):
    captured = {}
    class Input(io.BytesIO):
        def close(self):
            if not self.closed:
                captured['stdin'] = self.getvalue()
            super().close()
    class Process:
        def __init__(self, args, **kwargs):
            captured.update(args=args, kwargs=kwargs)
            self.stdin = Input()
            self.stdout = io.BytesIO(b'Enter pairing code: 123456\nSuccessfully paired to phone')
            self.returncode = 0
        def poll(self):
            return 0
    monkeypatch.setattr(shizuku, 'adb_path', lambda: str(tmp_path / 'adb.exe'))
    monkeypatch.setattr(shizuku.subprocess, 'Popen', Process)
    monkeypatch.setenv('ADB_SERVER_SOCKET', 'tcp:external-server:5037')
    monkeypatch.setenv('ADB_TRACE', 'all')
    job = SimpleNamespace(check_cancelled=lambda: None, cancel_event=threading.Event())
    code, output = shizuku.run_adb(job, ['pair', PAIR], input_text='123456')
    assert code == 0 and '123456' not in output and '验证码已隐藏' in output
    assert captured['args'] == [str(tmp_path / 'adb.exe'), 'pair', PAIR]
    assert captured['stdin'] == b'123456\n'
    assert 'shell' not in captured['kwargs']
    assert captured['kwargs']['creationflags'] == getattr(shizuku.subprocess, 'CREATE_NO_WINDOW', 0)
    assert 'ADB_SERVER_SOCKET' not in captured['kwargs']['env'] and 'ADB_TRACE' not in captured['kwargs']['env']


@pytest.mark.parametrize('cancel', [False, True])
def test_adb_runner_timeout_or_cancel_kills_only_client(monkeypatch, cancel):
    processes = []
    original = shizuku.subprocess.Popen
    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(shizuku, 'adb_path', lambda: sys.executable)
    monkeypatch.setattr(shizuku.subprocess, 'Popen', capture)
    event = threading.Event()
    def check():
        if event.is_set():
            raise Cancelled('cancelled fixture')
    job = SimpleNamespace(check_cancelled=check, cancel_event=event)
    timer = threading.Timer(.15, event.set) if cancel else None
    if timer:
        timer.start()
    try:
        with pytest.raises(Cancelled if cancel else TimeoutError):
            shizuku.run_adb(job, ['-c', 'import time; time.sleep(60)'], timeout=5 if cancel else .15)
    finally:
        if timer:
            timer.cancel()
    assert len(processes) == 1 and processes[0].poll() is not None
