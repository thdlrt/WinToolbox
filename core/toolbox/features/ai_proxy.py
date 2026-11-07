"""OpenAI-compatible loopback gateway. Upstream keys never leave the resident process."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from ..jobs import Cancelled
from ..settings import atomic_json
from .ai_calls import bounded_int

MAX_BODY = 16 * 1024 * 1024
OPTIONS = {'temperature', 'top_p', 'stop', 'seed', 'response_format', 'tool_choice', 'parallel_tool_calls',
           'presence_penalty', 'frequency_penalty', 'reasoning_effort', 'thinking', 'enable_thinking', 'top_k'}


class AiProxy:
    def __init__(self, app, calls):
        self.app, self.calls = app, calls
        self.path = app.data_dir / 'ai-proxy-local.json'
        self.server = self.thread = None
        self.error = None
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(6)
        self.stop = threading.Event()
        self.cache = {}
        self.catalogue_lock = threading.Lock()
        self.catalogue_path = app.data_dir / 'ai-proxy-models.json'
        self.catalogue = {'accounts': {}, 'updated_at': None, 'errors': []}
        if self.catalogue_path.exists():
            try:
                self.catalogue.update(json.loads(self.catalogue_path.read_text('utf-8')))
            except (ValueError, OSError):
                pass
        self.config = {'enabled': True, 'port': 18791}
        if self.path.exists():
            self.config.update(json.loads(self.path.read_text('utf-8')))
        bounded_int(self.config['port'], 1024, 65535, '本地端口')
        with app.storage.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ai_proxy_keys_v1(id TEXT PRIMARY KEY, record TEXT NOT NULL)')
        for method, handler in [('ai_proxy.status', self.status), ('ai_proxy.configure', self.configure),
                                ('ai_proxy.keys.create', self.create_key), ('ai_proxy.keys.revoke', self.revoke_key)]:
            app.register(method, handler)
        app.register('ai_proxy.models.refresh', self.submit_model_refresh)
        app.jobs.register('ai_proxy.models.refresh', self.refresh_models_job)

    def keys(self):
        with self.app.storage.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT record FROM ai_proxy_keys_v1')]

    def put_key(self, row):
        with self.app.storage.connect() as db:
            db.execute('INSERT OR REPLACE INTO ai_proxy_keys_v1(id,record) VALUES(?,?)', (row['id'], json.dumps(row, ensure_ascii=False)))

    def status(self, _=None):
        return {'enabled': self.config['enabled'], 'running': self.server is not None, 'port': self.config['port'],
                'base_url': f'http://127.0.0.1:{self.config["port"]}/v1', 'error': self.error,
                'max_request_bytes': MAX_BODY,
                'keys': [{k:v for k,v in r.items() if k != 'digest'} for r in self.keys()],
                'protocol': 'OpenAI Chat Completions', 'models': self.visible_models(),
                'models_refreshed_at': self.catalogue.get('updated_at'), 'model_errors': self.catalogue.get('errors', [])}

    def visible_models(self):
        result = []
        for route in self.calls.listing()['routes']:
            if not route['enabled'] or not route['ready']:
                continue
            saved = self.catalogue['accounts'].get(route['id'], {})
            ids = saved.get('models') if saved.get('base_url') == route['base_url'] else None
            result.extend(dict(id=self.alias(route, model), object='model', owned_by='wintoolbox', created=0,
                               provider=route['provider'], account_name=route['name']) for model in (ids or [route['default_model']]))
        return sorted(result, key=lambda r: (r['provider'], r['id']))

    def submit_model_refresh(self, p):
        if p:
            raise ValueError('获取模型无需参数。')
        with self.app.jobs.lock:
            for job in self.app.jobs.active.values():
                if job.record['tool'] == 'ai_proxy.models.refresh':
                    return dict(job.record)
            return self.app.jobs.submit('ai_proxy.models.refresh', {})

    def refresh_models_job(self, job):
        self.refresh_catalogue(force=True, job=job)
        return self.status()

    def create_key(self, p):
        if set(p) - {'name'}:
            raise ValueError('接入 Key 创建参数无效。')
        name = p.get('name', '本机应用')
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('请填写 1–80 字符的应用名称。')
        with self.lock:
            if sum(not r.get('revoked') for r in self.keys()) >= 32:
                raise ValueError('最多保留 32 个有效接入 Key，请先撤销不用的 Key。')
            key = 'wt_'+secrets.token_urlsafe(32)
            row = dict(id=uuid.uuid4().hex, name=name.strip(), prefix=key[:9],
                       digest=hashlib.sha256(key.encode()).hexdigest(), created_at=time.time(), revoked=False)
            self.put_key(row)
        return {'id': row['id'], 'api_key': key, 'name': row['name'], 'base_url': self.status()['base_url']}

    def revoke_key(self, p):
        with self.lock:
            row = next((r for r in self.keys() if r['id'] == p['id']), None)
            if not row:
                raise ValueError('接入 Key 不存在。')
            row['revoked'] = True
            self.put_key(row)
        return self.status()

    def authorized(self, value):
        if not isinstance(value, str) or not value.startswith('Bearer ') or len(value) > 256:
            return False
        digest = hashlib.sha256(value[7:].encode()).hexdigest()
        return any(not r.get('revoked') and hmac.compare_digest(r['digest'], digest) for r in self.keys())

    def default_models(self):
        return [{'id': self.alias(r, r['default_model']), 'object': 'model', 'owned_by': 'wintoolbox', 'created': 0}
                for r in self.calls.listing()['routes'] if r['ready'] and r['enabled']]

    def alias(self, route, model):
        peers = [r for r in self.calls.quota._all() if r['provider'] == route['provider']]
        prefix = route['provider'] if len(peers) == 1 else route['provider']+'@'+route['id']
        return prefix+'/'+model

    def resolve(self, model):
        if not isinstance(model, str) or '/' not in model or len(model) > 256:
            raise ValueError('请使用 /v1/models 返回的模型 ID，例如 kimi_code/kimi-for-coding。')
        prefix, upstream = model.split('/', 1)
        if not upstream or any(c in upstream for c in '\r\n\x00'):
            raise ValueError('模型 ID 无效。')
        routes = [r for r in self.calls.listing()['routes'] if self.alias(r, upstream) == model]
        if len(routes) != 1 or not routes[0]['enabled'] or not routes[0]['ready']:
            raise ValueError('模型账户不可用，请检查 AI 聚合中的 API 配置。')
        return routes[0], upstream

    def refresh_catalogue(self, force=False, job=None):
        with self.catalogue_lock:
            routes = [r for r in self.calls.listing()['routes'] if r['ready'] and r['enabled']]
            def fetch(route):
                saved = self.catalogue['accounts'].get(route['id'], {})
                if not force and saved.get('base_url') == route['base_url'] and time.time()-saved.get('fetched_at', 0) < 300:
                    return route['id'], saved, None
                try:
                    if job:
                        job.check_cancelled()
                    query = type('ModelQuery', (), {'params': {'account_id': route['id']}, 'check_cancelled': lambda _: job.check_cancelled() if job else None})()
                    ids = [r['id'] for r in self.calls.models_job(query)['models']
                           if not r['id'].startswith('wan') and not any(x in r['id'] for x in ('image', 'tts', 'asr', 'realtime', 'video'))]
                    if not ids:
                        raise ValueError('平台返回的对话模型目录为空。')
                    return route['id'], {'base_url': route['base_url'], 'models': sorted(set(ids)), 'fetched_at': time.time()}, None
                except Cancelled:
                    raise
                except Exception:
                    return route['id'], saved or None, route['name']+'模型列表获取失败，已保留上次目录或默认模型。'
            errors = []
            with ThreadPoolExecutor(max_workers=3) as pool:
                for account_id, saved, error in pool.map(fetch, routes):
                    if saved:
                        self.catalogue['accounts'][account_id] = saved
                    if error:
                        errors.append(error)
            if job:
                job.check_cancelled()
            stamps = [r.get('fetched_at', 0) for r in self.catalogue['accounts'].values()]
            self.catalogue.update(updated_at=max(stamps) if stamps else None, errors=errors)
            atomic_json(self.catalogue_path, self.catalogue)

    def models(self):
        self.refresh_catalogue()
        return {'object': 'list', 'data': self.visible_models()}

    def task(self, body):
        allowed = OPTIONS | {'model', 'messages', 'tools', 'stream', 'stream_options', 'max_tokens', 'max_completion_tokens', 'n', 'user'}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError('请求包含尚不支持的参数，请使用 OpenAI Chat Completions。')
        if type(body.get('stream', False)) is not bool or body.get('n', 1) != 1:
            raise ValueError('stream 必须为布尔值，n 仅支持 1。')
        if body.get('stream_options') is not None and not isinstance(body['stream_options'], dict):
            raise ValueError('stream_options 须为对象。')
        route, model = self.resolve(body.get('model'))
        messages = body.get('messages')
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) or m.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool') for m in messages):
            raise ValueError('messages 须为非空消息列表。')
        tools = body.get('tools')
        if tools is not None and (not isinstance(tools, list) or len(tools) > 64):
            raise ValueError('tools 须为列表，最多 64 个。')
        return dict(account_id=route['id'], model=model, messages=messages, tools=tools, label=route['name'],
                    timeout=180, max_tokens=bounded_int(body.get('max_tokens', body.get('max_completion_tokens', 4096)), 1, 32768, '输出预算'),
                    options={k:v for k,v in body.items() if k in OPTIONS})

    def configure(self, p):
        if set(p) - {'enabled', 'port'}:
            raise ValueError('代理配置包含未知字段。')
        config = dict(self.config, **p)
        if type(config['enabled']) is not bool:
            raise ValueError('启用状态无效。')
        bounded_int(config['port'], 1024, 65535, '本地端口')
        self.close()
        self.config = config
        atomic_json(self.path, self.config)
        self.start()
        return self.status()

    def start(self):
        if self.server or not self.config['enabled']:
            return
        owner = self
        owner.stop = threading.Event()
        stop_signal = owner.stop

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.settimeout(15)

            def log_message(self, *args):
                pass

            def json_reply(self, status, body, request_id=None):
                raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                if request_id:
                    self.send_header('X-WinToolbox-Request-Id', request_id)
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                self.wfile.write(raw)

            def error_reply(self, status, message, request_id=None, upstream=None):
                error = {'message': message, 'type': 'wintoolbox_proxy_error', 'code': str(status)}
                if request_id:
                    error['request_id'] = request_id
                if upstream is not None:
                    error['upstream'] = upstream
                self.json_reply(status, {'error': error}, request_id=request_id)

            def permitted(self):
                hosts = (f'127.0.0.1:{owner.config["port"]}', f'localhost:{owner.config["port"]}')
                if self.headers.get('Host') not in hosts or self.headers.get('Origin') is not None:
                    self.error_reply(403, '仅允许本机应用接入。')
                    return False
                if not owner.authorized(self.headers.get('Authorization', '')):
                    self.error_reply(401, 'WinToolbox 接入 Key 无效或已撤销。')
                    return False
                return True

            def do_GET(self):
                if not self.permitted():
                    return
                if self.path != '/v1/models':
                    self.error_reply(404, '支持 GET /v1/models 和 POST /v1/chat/completions。')
                    return
                if not owner.slots.acquire(blocking=False):
                    self.error_reply(429, '本机代理并发已达 6，请稍后重试。')
                    return
                try:
                    self.json_reply(200, owner.models())
                finally:
                    owner.slots.release()

            def do_POST(self):
                if not self.permitted():
                    return
                if self.path != '/v1/chat/completions':
                    self.error_reply(404, '当前支持 OpenAI Chat Completions。')
                    return
                if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding') is not None:
                    self.error_reply(400, '请提交 application/json 和 Content-Length。')
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_BODY:
                        self.error_reply(413, '请求为空或超过 16 MiB。')
                        return
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError('请求不完整。')
                    body = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效数字。')))
                    task = owner.task(body)
                except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
                    self.error_reply(400, str(exc))
                    return
                if not owner.slots.acquire(blocking=False):
                    self.error_reply(429, '本机代理并发已达 6，请稍后重试。')
                    return
                sent = False
                request_id = 'proxy-'+uuid.uuid4().hex
                stream = body.get('stream', False)
                def event(value):
                    nonlocal sent
                    if not sent:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-WinToolbox-Request-Id', request_id)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.close_connection = True
                        sent = True
                    try:
                        self.wfile.write(('data: '+json.dumps(dict(value, model=body['model']), ensure_ascii=False)+'\n\n').encode())
                        self.wfile.flush()
                    except OSError:
                        raise Cancelled('接入应用已断开。') from None
                job = type('ProxyCall', (), {'check_cancelled': lambda _: (_ for _ in ()).throw(Cancelled()) if stop_signal.is_set() else None})()
                try:
                    row = owner.calls.execute(task, request_id, 0, job, on_event=event if stream else None)
                    if row['status'] not in ('success', 'incomplete'):
                        if sent:
                            event({'error': {'message': row['error'], 'type': 'upstream_error',
                                             'request_id': request_id, 'upstream': row.get('upstream_error')}})
                        else:
                            self.error_reply(row.get('http_status') or 502, row['error'] or '模型调用中止。',
                                             request_id=request_id, upstream=row.get('upstream_error'))
                            return
                    if stream:
                        if not sent:
                            event({'choices': []})
                        self.wfile.write(b'data: [DONE]\n\n')
                        self.wfile.flush()
                    else:
                        message = {'role': 'assistant', 'content': row['text'] or None}
                        if row['reasoning']:
                            message['reasoning_content'] = row['reasoning']
                        if row['tool_calls']:
                            message['tool_calls'] = row['tool_calls']
                        result = dict(id=request_id, object='chat.completion', created=int(row['started_at']), model=body['model'],
                                      choices=[{'index': 0, 'message': message, 'finish_reason': row['finish_reason']}])
                        if row['usage'] is not None:
                            result['usage'] = row['usage']
                        self.json_reply(200, result, request_id=request_id)
                except (OSError, Cancelled):
                    pass
                finally:
                    owner.slots.release()

        try:
            self.server = ThreadingHTTPServer(('127.0.0.1', self.config['port']), Handler)
            self.server.daemon_threads = True
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .2}, daemon=True, name='ai-proxy')
            self.thread.start()
            self.error = None
        except OSError:
            self.server = None
            self.error = '本地端口不可用，请修改端口后重试。'

    def close(self):
        self.stop.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = self.thread = None
