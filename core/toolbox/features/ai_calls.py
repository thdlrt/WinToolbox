"""Concurrent development-model calls bound to the quota accounts, via local RPC/CLI."""
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from ..jobs import Cancelled
from ..settings import protect


PRESETS = {
    'kimi_code': {'base_url': 'https://api.kimi.com/coding/v1', 'model': 'kimi-for-coding'},
    'qianwen': {'base_url': 'https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1', 'model': 'qwen3.8-flash'},
    'opencode_go': {'base_url': 'https://opencode.ai/inference/go/openai/v1', 'model': 'deepseek-v4.1-flash',
                    'models_url': 'https://opencode.ai/inference/go/v1/models'},
    'glm': {'base_url': 'https://open.bigmodel.cn/api/coding/paas/v4', 'model': 'glm-5.3'},
}
MAX_RESPONSE = 4 * 1024 * 1024
MAX_ERROR_BODY = 8192
ERROR_ID_HEADERS = ('x-request-id', 'request-id', 'x-amzn-requestid', 'cf-ray')


def safe_error_text(value, key='', limit=600):
    """Only bounded diagnostic fields, never arbitrary bodies or request echoes."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value)
    if len(text) > MAX_ERROR_BODY:
        return '[diagnostic field omitted: too long]'
    if key:
        text = text.replace(key, '[redacted]')
    # Providers sometimes echo the submitted JSON inside an error message.
    if re.search(r'["\'](?:messages|tools|role|image_url|input)["\']\s*:', text, re.I):
        return '[request echo omitted]'
    text = re.sub(r'data:[^\s"\']{0,100};base64,[A-Za-z0-9+/=_-]+', '[image data omitted]', text, flags=re.I)
    text = re.sub(r'\bBearer\s+[^\s,;"\'}]+', 'Bearer [redacted]', text, flags=re.I)
    text = re.sub(r'\b(?:oc_sk_|wt_|sk-)[A-Za-z0-9_-]+', '[redacted]', text)
    text = re.sub(r'(?i)(authorization|api[_ -]?key|access[_ -]?token|secret)(["\']?\s*[:=]\s*["\']?)[^\s,;"\'}]+', r'\1\2[redacted]', text)
    text = re.sub(r'[A-Za-z0-9+/=_-]{160,}', '[long encoded value omitted]', text)
    return text[:limit]


def error_diagnostics(response, key='', payload=None, phase='http'):
    """Extract safe structured error fields; raw upstream bytes are never stored."""
    result = {'phase': phase, 'upstream_http_status': response.status_code, 'request_ids': {}}
    for name in ERROR_ID_HEADERS:
        value = response.headers.get(name)
        if value and len(value) <= 128 and re.fullmatch(r'[A-Za-z0-9_.:/-]+', value):
            result['request_ids'][name] = safe_error_text(value, key, 128)
    if payload is None:
        raw = bytearray()
        try:
            for part in response.iter_bytes(chunk_size=1024):
                raw.extend(part[:MAX_ERROR_BODY + 1 - len(raw)])
                if len(raw) > MAX_ERROR_BODY:
                    break
            result.update(body_bytes_read=len(raw), truncated=len(raw) > MAX_ERROR_BODY)
            if result['truncated']:
                result['body_status'] = 'truncated_not_retained'
                return result
            if not raw:
                result['body_status'] = 'empty'
                return result
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                result['body_status'] = 'non_json_not_retained'
                return result
        except Exception as exc:
            result.update(body_status='read_failed', read_error_type=type(exc).__name__)
            return result
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        result['body_status'] = 'unsupported_shape_not_retained'
        return result
    fields = {name: safe_error_text(error[name], key, 600 if name == 'message' else 128)
              for name in ('type', 'code', 'message') if name in error}
    result.update(body_status='structured_fields_only', error={k:v for k,v in fields.items() if v is not None})
    return result


class CallError(RuntimeError):
    def __init__(self, message, status_code=502, diagnostics=None):
        super().__init__(message)
        self.status_code = status_code
        self.diagnostics = diagnostics


def merge_tool_call_fragments(calls, fragments, upstream_http_status):
    """Null delta fields mean no new fragment; other malformed types fail safely."""
    def invalid(field, value):
        message = '平台工具调用片段格式无效：'+field+'。'
        raise CallError(message, diagnostics={
            'phase': 'stream_parse', 'upstream_http_status': upstream_http_status,
            'error': {'type': 'invalid_tool_call_fragment', 'code': 'invalid_tool_call_fragment', 'message': message},
            'field': field, 'value_type': type(value).__name__,
        })
    if fragments is None:
        return
    if not isinstance(fragments, list):
        invalid('tool_calls', fragments)
    for fragment in fragments:
        if not isinstance(fragment, dict):
            invalid('tool_calls.item', fragment)
        index = fragment.get('index', 0)
        if type(index) is not int or index < 0:
            invalid('tool_calls.index', index)
        call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
        call_id = fragment.get('id')
        if call_id is not None:
            if not isinstance(call_id, str):
                invalid('tool_calls.id', call_id)
            if call_id:
                call['id'] = call_id
        function = fragment.get('function')
        if function is None:
            continue
        if not isinstance(function, dict):
            invalid('tool_calls.function', function)
        for field in ('name', 'arguments'):
            value = function.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                invalid('tool_calls.function.'+field, value)
            call['function'][field] += value


def bounded_int(value, lower, upper, name):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{name}须为 {lower}–{upper} 的整数。')
    return value


class AiCalls:
    def __init__(self, app, quota, transport=None):
        self.app, self.quota, self.transport = app, quota, transport
        self.lock = threading.RLock()
        with app.storage.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ai_call_routes_v1(id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS ai_call_batches_v1(id TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS ai_call_results_v1(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS ai_call_batch_results ON ai_call_results_v1(batch_id)')

    def _get(self, table, key):
        with self.app.storage.connect() as db:
            row = db.execute(f'SELECT record FROM {table} WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, table, key, row, batch_id=None):
        with self.app.storage.connect() as db:
            if batch_id is None:
                db.execute(f'INSERT OR REPLACE INTO {table}(id,record) VALUES(?,?)', (key, json.dumps(row, ensure_ascii=False)))
            else:
                db.execute(f'INSERT OR REPLACE INTO {table}(id,batch_id,record) VALUES(?,?,?)', (key, batch_id, json.dumps(row, ensure_ascii=False)))

    def route(self, account_id):
        account = self.quota._get(account_id)
        if not account or account['provider'] not in PRESETS:
            raise ValueError('请选择 Kimi Code、千问 Token Plan 或 OpenCode Go 额度账户。')
        saved = self._get('ai_call_routes_v1', account_id) or {}
        provider = account['provider']
        return dict(id=account_id, name=account['name'], provider=provider, **PRESETS[provider],
                    enabled=saved.get('enabled', True), source_provider_id=saved.get('source_provider_id', ''),
                    credential=saved.get('credential'), default_model=saved.get('model', PRESETS[provider]['model']))

    def key(self, route):
        if route.get('source_provider_id'):
            setting = next((p for p in self.app.settings.get()['providers'] if p['id'] == route['source_provider_id']), None)
            if not setting or setting.get('base_url', '').rstrip('/') != route['base_url']:
                raise CallError('绑定的模型服务已删除或地址改变，请重新设置调用凭据。')
            value = self.app.settings.secret(setting['id'])
        elif route.get('credential'):
            value = protect(route['credential'], decrypt=True)
        elif route['provider'] != 'qianwen':
            account = self.quota._get(route['id'])
            if account.get('source_provider_id'):
                setting = next((p for p in self.app.settings.get()['providers'] if p['id'] == account['source_provider_id']), None)
                if not setting or not self.quota.matches(route['provider'], setting):
                    raise CallError('额度账户绑定的模型服务已改变，请重新连接。')
                value = self.app.settings.secret(account['source_provider_id'])
            else:
                value = protect(account.get('credential', ''), decrypt=True)
        else:
            raise CallError('千问额度查询 Cookie 不能调用模型，请填写 Token Plan 专用 API Key。')
        if not value:
            raise CallError('未配置模型调用 API Key。')
        return value

    def listing(self, _=None):
        values = self.app.settings.get()['providers']
        rows = []
        for account in self.quota._all():
            if account['provider'] not in PRESETS:
                continue
            route = self.route(account['id'])
            route.pop('credential')
            try:
                self.key(self.route(account['id']))
                route.update(ready=True, error=None)
            except Exception as exc:
                route.update(ready=False, error=str(exc) if isinstance(exc, CallError) else '无法读取调用密钥，请重新配置。')
            route['sources'] = [dict(id=p['id'], name=p['name']) for p in values
                                if p.get('has_key') and p.get('base_url', '').rstrip('/') == route['base_url']]
            route['usage'] = self.app.storage.usage_summary('ai_calls:'+route['id'])['periods']['all']
            rows.append(route)
        return {'routes': rows}

    def save(self, p):
        if set(p) - {'account_id', 'enabled', 'model', 'source_provider_id', 'api_key'}:
            raise ValueError('调用配置包含未知字段。')
        route = self.route(p['account_id'])
        old = self._get('ai_call_routes_v1', route['id']) or {}
        model = p.get('model', route['default_model'])
        if not isinstance(model, str) or not model.strip() or len(model) > 160:
            raise ValueError('请填写有效模型 ID。')
        enabled = p.get('enabled', route['enabled'])
        if type(enabled) is not bool:
            raise ValueError('启用状态必须为布尔值。')
        source = p.get('source_provider_id', old.get('source_provider_id', ''))
        key = p.get('api_key', '')
        if not isinstance(key, str) or len(key) > 4096 or any(c in key for c in '\r\n\x00'):
            raise ValueError('API Key 格式无效。')
        if source and key.strip():
            raise ValueError('请选择一种密钥来源。')
        row = dict(enabled=enabled, model=model.strip(), source_provider_id=source,
                   credential=None if source else protect(key.strip()) if key.strip() else old.get('credential'))
        if source:
            self.key(dict(route, **row))
        self._put('ai_call_routes_v1', route['id'], row)
        return next(r for r in self.listing()['routes'] if r['id'] == route['id'])

    @staticmethod
    def _check(response, key=''):
        if response.status_code >= 300:
            hints = {401: '密钥无效', 403: '平台拒绝当前调用身份或账户权限', 429: '额度或并发受限'}
            raise CallError(f'模型接口 HTTP {response.status_code}：'+hints.get(response.status_code, '请求失败，请核对模型及平台状态'),
                            response.status_code, error_diagnostics(response, key))

    def models_job(self, job):
        route = self.route(job.params['account_id'])
        key = self.key(route)
        with httpx.Client(timeout=20, transport=self.transport, follow_redirects=False) as client:
            with client.stream('GET', route.get('models_url', route['base_url']+'/models'), headers=self.headers(key)) as response:
                self._check(response, key)
                raw = bytearray()
                for part in response.iter_bytes():
                    job.check_cancelled()
                    raw.extend(part)
                    if len(raw) > MAX_RESPONSE:
                        raise CallError('模型列表超过限制。')
        body = json.loads(raw)
        rows = body.get('data', body.get('models', []))
        if not isinstance(rows, list):
            raise CallError('平台未返回模型列表。')
        return {'account_id': route['id'], 'models': [{'id': r['id']} for r in rows if isinstance(r, dict) and isinstance(r.get('id'), str)]}

    @staticmethod
    def headers(key):
        # The genuine client identity is retained. Never impersonate a whitelisted agent.
        return {'Authorization': 'Bearer '+key, 'User-Agent': 'WinToolbox/0.6 development-agent', 'Accept': 'application/json'}

    def submit(self, p):
        if set(p) - {'request_id', 'tasks', 'concurrency', 'project'}:
            raise ValueError('批量调用包含未知字段。')
        request_id = p.get('request_id')
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise ValueError('请提供 8–128 字符的稳定 request_id，重试时沿用。')
        concurrency = bounded_int(p.get('concurrency', 3), 1, 6, '并发数')
        tasks = p.get('tasks')
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12:
            raise ValueError('一次提交 1–12 个模型任务。')
        project = p.get('project', '')
        if not isinstance(project, str) or len(project) > 512:
            raise ValueError('项目标记过长或格式无效。')
        checked = []
        for item in tasks:
            if not isinstance(item, dict) or set(item) - {'account_id', 'model', 'prompt', 'messages', 'tools', 'max_tokens', 'timeout', 'label'}:
                raise ValueError('模型任务包含未知字段。')
            route = self.route(item.get('account_id'))
            if not route['enabled']:
                raise ValueError('此账户已停用模型调用。')
            messages = item.get('messages')
            if messages is None and isinstance(item.get('prompt'), str):
                messages = [{'role': 'user', 'content': item['prompt']}]
            if not isinstance(messages, list) or not messages or len(json.dumps(messages, ensure_ascii=False)) > 500000:
                raise ValueError('请提供消息列表或任务文字，单任务不超过 500000 字符。')
            if any(not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant', 'tool') for m in messages):
                raise ValueError('消息角色无效。')
            model = item.get('model', route['default_model'])
            if not isinstance(model, str) or not model.strip() or len(model) > 160:
                raise ValueError('模型 ID 无效。')
            tools = item.get('tools')
            if tools is not None and (not isinstance(tools, list) or len(tools) > 64):
                raise ValueError('工具声明须为列表，最多 64 个。')
            checked.append(dict(account_id=route['id'], model=model, messages=messages, tools=tools,
                                max_tokens=bounded_int(item.get('max_tokens', 4096), 32, 32768, '输出预算'),
                                timeout=bounded_int(item.get('timeout', 180), 5, 600, '超时秒数'),
                                label=str(item.get('label', route['name']))[:120]))
        spec = dict(tasks=checked, concurrency=concurrency, project=project)
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            old = self._get('ai_call_batches_v1', request_id)
            if old:
                if old['digest'] != digest:
                    raise ValueError('此 request_id 已用于其他参数，请查询原任务或使用新 ID。')
                job = self.app.jobs.get(old['job_id']) if old.get('job_id') else None
                if job:
                    return job
                raise CallError('原提交状态不确定，请先查询 ai_calls.get，不自动重新请求平台。')
            row = dict(id=request_id, digest=digest, spec=spec, job_id=None, created_at=time.time())
            self._put('ai_call_batches_v1', request_id, row)
            job = self.app.jobs.submit('ai_calls.run', {'request_id': request_id})
            row['job_id'] = job['id']
            self._put('ai_call_batches_v1', request_id, row)
            return job

    def run_job(self, job):
        batch = self._get('ai_call_batches_v1', job.params['request_id'])
        if not batch:
            raise ValueError('批次不存在，请通过 ai_calls.submit 提交。')
        # A retry of a completed/interrupted job must not silently spend quota again.
        with self.lock:
            if batch.get('started_at'):
                raise CallError('本批次已开始过；请查询已有结果。重新调用需新 request_id。')
            batch['started_at'] = time.time()
            self._put('ai_call_batches_v1', batch['id'], batch)
        tasks = batch['spec']['tasks']
        results = [None]*len(tasks)
        with ThreadPoolExecutor(max_workers=batch['spec']['concurrency'], thread_name_prefix='ai-call') as pool:
            pending = {pool.submit(self.execute, task, batch['id'], i, job): i for i, task in enumerate(tasks)}
            for future in as_completed(pending):
                i = pending[future]
                results[i] = future.result()
                if not job.cancel_event.is_set():
                    job.progress(sum(r is not None for r in results)/len(tasks)*100, '模型任务已返回')
        job.check_cancelled()
        return {'request_id': batch['id'], 'results': results,
                'successful': sum(r['status'] == 'success' for r in results),
                'failed': sum(r['status'] != 'success' for r in results)}

    def execute(self, task, batch_id, index, job, on_event=None):
        started = time.monotonic()
        calls = {}
        row = dict(id=batch_id+':'+str(index), request_id=batch_id, index=index, account_id=task['account_id'],
                   requested_model=task['model'], model=None, label=task['label'], status='running',
                   started_at=time.time(), text='', reasoning='', tool_calls=[], finish_reason=None,
                   usage=None, input_tokens=None, output_tokens=None, total_tokens=None,
                   elapsed_ms=None, ttft_ms=None, tps=None, actual_cost=None, error=None, http_status=None, upstream_error=None)
        self._put('ai_call_results_v1', row['id'], row, batch_id)
        try:
            job.check_cancelled()
            route = self.route(task['account_id'])
            if not route['enabled']:
                raise CallError('此账户已停用模型调用。')
            key = self.key(route)
            payload = dict(model=task['model'], messages=task['messages'], stream=True,
                           stream_options={'include_usage': True}, max_tokens=task['max_tokens'])
            headers = self.headers(key)
            if route['provider'] == 'opencode_go':
                headers['x-opencode-session'] = 'wintoolbox-'+hashlib.sha256(batch_id.encode()).hexdigest()[:32]
            if task.get('tools'):
                payload['tools'] = task['tools']
            payload.update(task.get('options') or {})
            calls, finished, saw_done, size = {}, False, False, 0
            with httpx.Client(timeout=httpx.Timeout(task['timeout'], connect=15), transport=self.transport, follow_redirects=False) as client:
                with client.stream('POST', route['base_url']+'/chat/completions', headers=headers, json=payload) as response:
                    self._check(response, key)
                    for line in response.iter_lines():
                        job.check_cancelled()
                        if time.monotonic()-started > task['timeout']:
                            raise CallError('模型调用超过总时间限制。')
                        size += len(line.encode('utf-8'))
                        if size > MAX_RESPONSE:
                            raise CallError('模型响应超过 4 MiB 限制。')
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            saw_done = True
                            break
                        event = json.loads(data)
                        if event.get('error'):
                            raise CallError('平台在流式响应中返回错误。', diagnostics=error_diagnostics(response, key, event, phase='stream'))
                        row['model'] = event.get('model') or row['model']
                        if isinstance(event.get('usage'), dict):
                            row['usage'] = event['usage']
                        for choice in event.get('choices') or []:
                            if choice.get('index', 0) != 0:
                                continue
                            delta = choice.get('delta') or {}
                            for field, target in [('content', 'text'), ('reasoning_content', 'reasoning')]:
                                if isinstance(delta.get(field), str):
                                    row[target] += delta[field]
                                    if target == 'text' and row['ttft_ms'] is None:
                                        row['ttft_ms'] = round((time.monotonic()-started)*1000)
                            merge_tool_call_fragments(calls, delta.get('tool_calls'), response.status_code)
                            if choice.get('finish_reason'):
                                row['finish_reason'] = choice['finish_reason']
                                finished = True
                        if on_event:
                            on_event(event)
            row['tool_calls'] = list(calls.values())
            if not finished or not saw_done:
                raise CallError('流式响应未完整结束；保留部分输出，不自动重试。')
            row['status'] = 'success' if row['finish_reason'] in ('stop', 'tool_calls') else 'incomplete'
            if row['status'] == 'incomplete':
                row['error'] = '模型输出未完成：'+row['finish_reason']
        except Cancelled:
            row.update(status='cancelled', error='任务已取消。')
        except Exception as exc:
            row.update(status='error', error=str(exc) if isinstance(exc, CallError) else '模型连接或响应解析失败（'+type(exc).__name__+'）。',
                       http_status=exc.status_code if isinstance(exc, CallError) else 502,
                       upstream_error=exc.diagnostics if isinstance(exc, CallError) else None)
        finally:
            row['tool_calls'] = list(calls.values())
            row['elapsed_ms'] = max(1, round((time.monotonic()-started)*1000))
            usage = row['usage'] or {}
            for target, names in [('input_tokens', ('prompt_tokens', 'input_tokens')), ('output_tokens', ('completion_tokens', 'output_tokens')), ('total_tokens', ('total_tokens',))]:
                row[target] = next((usage[n] for n in names if type(usage.get(n)) is int and usage[n] >= 0), None)
            if row['output_tokens'] is not None:
                row['tps'] = round(row['output_tokens']*1000/row['elapsed_ms'], 2)
            self._put('ai_call_results_v1', row['id'], row, batch_id)
            self.app.storage.record_usage('ai_calls:'+task['account_id'], task['model'], 'development', 'ai_calls',
                                          'success' if row['status'] == 'success' else 'cancelled' if row['status'] == 'cancelled' else 'error',
                                          usage=usage, latency_ms=row['elapsed_ms'])
        return row

    def get(self, p):
        request_id = p['request_id']
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError('请求 ID 无效。')
        batch = self._get('ai_call_batches_v1', request_id)
        if not batch:
            # Proxy calls use the same result ledger but have no batch/job record.
            # Exact ID lookup only; never return input specs, messages or credentials.
            if re.fullmatch(r'proxy-[a-f0-9]{32}', request_id):
                row = self._get('ai_call_results_v1', request_id+':0')
                if row and row.get('request_id') == request_id and row.get('index') == 0:
                    fields = {'id', 'request_id', 'index', 'account_id', 'requested_model', 'model', 'label',
                              'status', 'started_at', 'text', 'reasoning', 'tool_calls', 'finish_reason', 'usage',
                              'input_tokens', 'output_tokens', 'total_tokens', 'elapsed_ms', 'ttft_ms', 'tps',
                              'actual_cost', 'error', 'http_status', 'upstream_error'}
                    return {'request_id': request_id, 'source': 'proxy', 'job_id': None, 'project': '',
                            'results': [{k:v for k,v in row.items() if k in fields}], 'job': None}
            raise ValueError('批次不存在。')
        with self.app.storage.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT record FROM ai_call_results_v1 WHERE batch_id=?', (batch['id'],))]
        job = self.app.jobs.get(batch['job_id']) if batch['job_id'] else None
        if job and job['status'] not in ('queued', 'running', 'cancelling'):
            for row in rows:
                if row['status'] == 'running':
                    row.update(status='interrupted', error='调用进程已结束，平台用量和完成状态未知，不自动重试。')
        return {'request_id': batch['id'], 'job_id': batch['job_id'], 'project': batch['spec']['project'],
                'results': sorted(rows, key=lambda r: r['index']),
                'job': job}


def register(app, quota):
    feature = AiCalls(app, quota)
    from .ai_proxy import AiProxy
    app.ai_proxy = AiProxy(app, feature)
    app.register('ai_calls.list', feature.listing)
    app.register('ai_calls.save', feature.save)
    app.register('ai_calls.submit', feature.submit)
    app.register('ai_calls.get', feature.get)
    app.register('ai_calls.models', lambda p: app.jobs.submit('ai_calls.models', {'account_id': feature.route(p['account_id'])['id']}))
    app.jobs.register('ai_calls.models', feature.models_job)
    app.jobs.register('ai_calls.run', feature.run_job)
