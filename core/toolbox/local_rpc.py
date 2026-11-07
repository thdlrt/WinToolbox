"""Authenticated, loopback-only access to the resident desktop backend."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import uuid

from .settings import atomic_json, protect

ENDPOINT_FILE = 'cli-runtime.json'
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
# Native shutdown owns the process lifecycle. Calling it over RPC would close
# the transport before its response and stop unrelated background work.
INTERNAL_METHODS = {'app.prepare_exit'}


class LocalRpcServer:
    def __init__(self, app, *, seal=protect):
        self.app = app
        self.path = Path(app.data_dir) / ENDPOINT_FILE
        self.seal = seal
        self.token = secrets.token_urlsafe(32)
        self.instance = uuid.uuid4().hex
        self.server = self.thread = None

    def dispatch(self, request):
        if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                or not isinstance(request.get('method'), str)
                or not isinstance(request.get('params', {}), dict)
                or type(request.get('id')) not in (str, int)):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': '无效 JSON-RPC 请求'}}
        method, params = request['method'], request.get('params', {})
        response = {'jsonrpc': '2.0', 'id': request['id']}
        try:
            if method in INTERNAL_METHODS:
                raise LookupError('此方法由桌面程序管理，不能通过 CLI 调用')
            if method == 'cli.methods':
                prefix = params.get('prefix', '')
                if not isinstance(prefix, str):
                    raise ValueError('方法前缀必须为文本')
                response['result'] = {'methods': sorted(name for name in (*self.app.handlers, 'cli.methods')
                                                       if name not in INTERNAL_METHODS and name.startswith(prefix))}
            else:
                response['result'] = self.app.call(method, params)
        except Exception as exc:
            code = -32601 if isinstance(exc, LookupError) else -32602 if isinstance(exc, (ValueError, KeyError, TypeError)) else -32000
            response['error'] = {'code': code, 'message': str(exc)}
        return response

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(15)

            def log_message(self, *args):
                pass  # Never log credentials, request bodies, or private results.

            def reply(self, status, value):
                raw = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode('utf-8')
                if len(raw) > MAX_RESPONSE:
                    status, raw = 413, b'{"error":"RPC response too large"}'
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                expected_host = '127.0.0.1:' + str(owner.server.server_port)
                if (self.path != '/rpc' or self.headers.get('Origin') is not None
                        or self.headers.get('Host') != expected_host
                        or self.headers.get('Transfer-Encoding') is not None):
                    self.reply(403, {'error': '仅允许本机 CLI 请求'})
                    return
                auth = self.headers.get('Authorization', '')
                if not hmac.compare_digest(auth.encode('utf-8'), ('Bearer ' + owner.token).encode('ascii')):
                    self.reply(401, {'error': 'CLI 身份验证失败，请使用当前 Windows 用户连接'})
                    return
                if self.headers.get_content_type() != 'application/json':
                    self.reply(415, {'error': '请求必须为 application/json'})
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_REQUEST:
                        self.reply(413, {'error': '请求为空或超过 1 MiB'})
                        return
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError()
                    request = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效数字')))
                except (ValueError, UnicodeDecodeError, TimeoutError):
                    self.reply(400, {'error': '无效或不完整的 JSON 请求'})
                    return
                self.reply(200, owner.dispatch(request))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        try:
            # Outside all managed backup/sync roots; token is current-user DPAPI.
            atomic_json(self.path, {'protocol': 1, 'host': '127.0.0.1', 'port': self.server.server_port,
                                    'pid': os.getpid(), 'instance': self.instance,
                                    'token_dpapi': self.seal(self.token)})
        except Exception:
            self.server.server_close()
            self.server = None
            raise
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .2}, daemon=True, name='toolbox-cli')
        self.thread.start()

    def close(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = None
        try:
            if json.loads(self.path.read_text('utf-8')).get('instance') == self.instance:
                self.path.unlink()
        except (FileNotFoundError, ValueError):
            pass
