"""Diagnostics are tested against isolated loopback fixtures, never user services."""
import http.server
import socket
import threading
import time
import datetime
import ssl

import pytest

from toolbox.features.network import diagnose, resolve, target_options
from toolbox.jobs import Cancelled


class Job:
    def progress(self, *_):
        self.check_cancelled()
    def check_cancelled(self):
        pass


@pytest.mark.parametrize('target', ['https://user:pass@example.com', 'https://example.com/?token=secret',
                                  'https://example.com/#secret', 'file:///tmp/a', 'host\r\nHeader:x', 'host:22'])
def test_secret_or_invalid_targets_rejected(target):
    with pytest.raises(ValueError):
        target_options({'target': target})


def test_hostname_ip_and_url_port():
    assert target_options({'target': 'https://example.com:8443/check'})['port'] == 8443
    assert target_options({'target': '::1', 'port': 22})['host'] == '::1'
    assert target_options({'target': 'http://example.com'})['port'] == 80
    for values in ({'port': True}, {'attempts': 0}, {'timeout_ms': 100}, {'port': 65536}):
        with pytest.raises(ValueError):
            target_options({'target': 'localhost', **values})


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(405 if self.path == '/fallback' else 401 if self.path == '/auth' else 302 if self.path == '/redirect' else 200)
            if self.path == '/redirect':
                self.send_header('Location', 'http://never-contact.invalid')
            self.end_headers()
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass
    service = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{service.server_port}'
    service.shutdown()
    service.server_close()
    thread.join(2)


@pytest.mark.parametrize('path,code', [('/', 200), ('/fallback', 200), ('/auth', 401), ('/redirect', 302)])
def test_http_and_tcp_report_real_results(server, path, code):
    report = diagnose({'target': server+path, 'attempts': 2}, Job())
    steps = {s['name']: s for s in report['steps']}
    assert steps['dns']['status'] == 'ok'
    assert steps['tcp']['success_count'] == 2
    assert len(steps['tcp']['samples_ms']) == 2
    assert steps['tls']['status'] == 'skipped'
    assert steps['http']['http_status'] == code
    assert steps['http']['status'] == ('error' if code == 401 else 'ok')


def test_refused_port_skips_http():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    try:
        result = diagnose({'target': 'http://127.0.0.1', 'port': port, 'attempts': 1, 'timeout_ms': 1000}, Job())
        assert result['steps'][1]['status'] == 'error'
        assert result['steps'][-1]['status'] == 'skipped'
    finally:
        sock.close()


def test_dns_failure_has_specific_result(monkeypatch):
    def fail(*_, **__):
        raise socket.gaierror('fixture')
    monkeypatch.setattr(socket, 'getaddrinfo', fail)
    result = diagnose({'target': 'test.invalid'}, Job())
    assert result['steps'][0]['status'] == 'error'
    assert all(s['status'] == 'skipped' for s in result['steps'][1:])


def test_dns_wait_bounded_and_cancellable(monkeypatch):
    released = threading.Event()
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_, **__: released.wait(2))
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            resolve('fixture', 80, .1, lambda: None)
        assert time.monotonic() - start < .5
        def cancel():
            raise Cancelled()
        with pytest.raises(Cancelled):
            resolve('fixture', 80, 5, cancel)
    finally:
        released.set()


def test_tls_verifies_certificate_and_reports_http(tmp_path, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'fixture.local')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path/'cert.pem', tmp_path/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_):
            pass
    service = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    service.socket = context.wrap_socket(service.socket, server_side=True)
    worker = threading.Thread(target=service.serve_forever, daemon=True)
    worker.start()
    try:
        params = {'target': f'https://127.0.0.1:{service.server_port}', 'attempts': 1}
        result = diagnose(params, Job())
        assert result['steps'][2]['status'] == 'error'
        assert '证书' in result['steps'][2]['detail']
        assert result['steps'][3]['status'] == 'skipped'
        default = ssl.create_default_context
        monkeypatch.setattr(ssl, 'create_default_context', lambda: default(cafile=str(certfile)))
        result = diagnose(params, Job())
        assert result['steps'][2]['status'] == 'ok'
        assert result['steps'][3]['http_status'] == 200
    finally:
        service.shutdown()
        service.server_close()
        worker.join(2)


def test_profiles_use_ledger_and_reject_stale_updates(tmp_path):
    from toolbox.app import App
    app = App(tmp_path / 'data', register_live=False)
    try:
        saved = app.call('network.profiles.save', {'name': '本机', 'target': 'http://127.0.0.1', 'attempts': 1})
        assert app.call('network.profiles.list')['profiles'][0]['id'] == saved['id']
        assert app.ledger.get('network_profile', saved['id'])['target'] == 'http://127.0.0.1'
        edited = app.call('network.profiles.save', {**saved, 'name': '新名称'})
        assert edited['revision'] != saved['revision']
        with pytest.raises(ValueError, match='已更新'):
            app.call('network.profiles.save', saved)
        with pytest.raises(ValueError, match='已更新'):
            app.call('network.profiles.delete', {'id': saved['id'], 'revision': saved['revision']})
        app.call('network.profiles.delete', {'id': edited['id'], 'revision': edited['revision']})
        assert not app.call('network.profiles.list')['profiles']
        assert app.ledger.get('network_profile', saved['id'])['deleted']
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)
