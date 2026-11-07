"""Read the running local DSH through its existing Pocket authenticated proxy.

No chat creation, prompts, approvals, config edits or extra listener. Credentials
and browser cookies stay in this process; only normalized rows leave this module.
"""
import http.client
from http.cookies import SimpleCookie
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode


class DshSessions:
    def __init__(self, home=None, port=3081):
        self.home = Path(home or os.environ.get('DSH_HOME') or Path.home() / '.dsh')
        self.port = port
        self.cookies = {}
        self.previous = {}
        self.ends = {}
        self.recent_done = {}

    def _request(self, method, path, body=None, content_type=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items())}
        if content_type:
            headers['Content-Type'] = content_type
        try:
            client.request(method, path, body, headers)
            response = client.getresponse()
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError('DSH 会话响应超过本机读取上限')
            for key, value in response.getheaders():
                if key.lower() == 'set-cookie':
                    parsed = SimpleCookie()
                    parsed.load(value)
                    self.cookies.update({k: v.value for k, v in parsed.items()})
            return response.status, raw
        finally:
            client.close()

    def _login(self):
        self.cookies.clear()
        pin_path = self.home / 'dsh-pocket/token-lan'
        if not pin_path.is_file():
            raise RuntimeError('DSH 需启用已有的 Pocket 本机入口')
        pin = pin_path.read_text('utf-8').strip()
        status, _ = self._request('POST', '/pocket-login', urlencode({'token': pin}),
                                  'application/x-www-form-urlencoded')
        if status not in (200, 302, 303):
            raise RuntimeError('DSH 本机入口认证未成功，请检查 Pocket 状态')
        # Standard Pocket login followed by the Host's launch-cookie exchange.
        # Redirects never leave loopback; no cookie/signature is manufactured.
        for _ in range(2):
            status, _ = self._request('GET', '/')
            if status not in (200, 302, 303):
                raise RuntimeError('DSH 本机入口尚未就绪')

    def rpc(self, method, args):
        if method not in ('session/list', 'session/page'):
            raise ValueError('DSH 任务台只允许读取会话')
        body = json.dumps({'type': 'client-request', 'rpcId': 'stackchan-read',
                           'method': method, 'payload': {'args': args}})
        if not self.cookies:
            self._login()
        status, raw = self._request('POST', '/api/' + method, body, 'application/json')
        if status == 401:
            self._login()
            status, raw = self._request('POST', '/api/' + method, body, 'application/json')
        if status != 200:
            raise RuntimeError(f'DSH 本机入口 HTTP {status}')
        value = json.loads(raw)
        result = value.get('result', {})
        if value.get('rpcId') != 'stackchan-read' or not result.get('ok'):
            raise RuntimeError('DSH 会话接口暂不可用或版本不兼容')
        return result['value']

    def read(self):
        raw = self.rpc('session/list', {'_request': {}})
        result = normalize(raw)
        try:
            workspace = json.loads((self.home / 'storages/workspace.json').read_text('utf-8'))
            archived = workspace.get('global', {}).get('archivedSessionIds', [])
            result['rows'] = [r for r in result['rows'] if r['session_id'] not in archived]
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        result['rows'].sort(key=lambda r: (r['state'] == 'idle', -r['updated_at']))
        result['rows'] = [r for i, r in enumerate(result['rows']) if i < 50 or r['state'] != 'idle']
        for row in result['rows']:
            key = row['id']
            before = self.previous.get(key)
            changed = not before or before['as_of_seq'] != row['as_of_seq'] or before['state'] != row['state']
            ended = before and row['state'] == 'idle' and (before['state'] in ('running', 'waiting') or
                                                            before['turn_start'] != row['turn_start'])
            recent_idle = row['state'] == 'idle' and 0 <= time.time() - row['updated_at'] <= 3600
            if changed and (row['state'] == 'running' or ended or recent_idle):
                try:
                    page = self.rpc('session/page', {'request': {
                        'address': {'kind': 'session', 'sessionId': row['session_id']},
                        'throughSeq': row['as_of_seq'], 'maxMessages': 2}})
                    if pending_approval(page) and row['state'] == 'running':
                        row['state'] = 'waiting'
                    if ended or recent_idle:
                        end = terminal_event(page, row['turn_start'])
                        if end:
                            self.recent_done[key] = (row['updated_at'], end)
                            if ended:
                                self.ends[key] = end
                except (OSError, ValueError, RuntimeError):
                    pass  # Idle remains idle when the termination cannot be verified.
            self.previous[key] = dict(row)
            end = self.ends.get(key)
            verified = self.recent_done.get(key)
            if verified and verified[0] == row['updated_at'] and verified[1]['kind'] == 'done':
                row['completed_at'] = verified[1].get('completed_at', 0)
            if end and end['seq'] > row['turn_start']:
                row['completion'] = end
                if end['kind'] == 'problem':
                    row['state'] = 'error'
        return result


def pending_approval(page):
    pending = set()
    for record in page.get('records', []):
        event = record.get('event', {})
        data = event.get('data', {})
        if event.get('type') == 'approval/asked':
            pending.add(data.get('id'))
        elif event.get('type') == 'approval/decided':
            pending.discard(data.get('id'))
    return bool(pending)


def terminal_event(page, turn_start):
    events = [r.get('event', {}) for r in page.get('records', []) if r.get('type') == 'event']
    for event in reversed(events):
        if event.get('type') != 'turn/end' or event.get('seq', -1) <= turn_start:
            continue
        reason = event.get('data', {}).get('reason', {}).get('kind')
        if reason not in ('completed', 'error'):
            return None
        texts = [b.get('text', '') for e in events if e.get('type') == 'assistant/message'
                 for b in e.get('data', {}).get('message', {}).get('content', []) if b.get('type') == 'text']
        text = texts[-1] if texts else ''
        sensitive = re.search(r'(?i)token|api[_ -]?key|secret|password|密码|密钥|bearer|sk[-_]|[A-Za-z0-9_-]{32,}|```', text)
        summary = ' '.join(text.split())[:60] if text and not sensitive else '本轮执行结束，请在 DSH 查看结果。'
        result = {'seq': event['seq'], 'kind': 'done' if reason == 'completed' else 'problem',
                  'summary': summary if reason == 'completed' else 'DSH 本轮执行异常，请回 PC 查看具体原因。'}
        ended_at = event.get('time')
        if type(ended_at) in (int, float) and math.isfinite(ended_at) and ended_at > 0:
            result['completed_at'] = ended_at / 1000 if ended_at > 100_000_000_000 else ended_at
        return result
    return None


def normalize(raw, now=None):
    if not isinstance(raw, dict) or not isinstance(raw.get('items'), list):
        raise ValueError('DSH 会话列表格式无效')
    rows = []
    for item in raw['items']:
        if not isinstance(item, dict) or item.get('blank') or not isinstance(item.get('sessionId'), str):
            continue
        values = item.get('projections', {}).get('values', {})
        questions = values.get('userQuestions', {}).get('active', [])
        state = 'waiting' if questions and item.get('agentAvailable') else 'running' if item.get('running') else 'idle'
        title = values.get('title') or '未命名会话'
        if not isinstance(title, str):
            title = '未命名会话'
        updated = item.get('updatedAt', 0)
        updated = updated / 1000 if type(updated) in (int, float) and math.isfinite(updated) else 0
        outline = values.get('turnOutline', [])
        if isinstance(outline, dict):
            outline = outline.get('turns', [])
        last = outline[-1] if isinstance(outline, list) and outline else {}
        rows.append({'id': 'dsh:' + item['sessionId'], 'session_id': item['sessionId'], 'source': 'dsh',
                     'title': '[DSH] ' + title[:72], 'state': state, 'updated_at': updated,
                     'turn_start': last.get('seq', -1), 'as_of_seq': item.get('projections', {}).get('asOfSeq', -1)})
    return {'available': True, 'sampled_at': time.time() if now is None else now, 'rows': rows}
