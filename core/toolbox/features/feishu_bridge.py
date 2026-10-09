"""Configurable Feishu event listener that forwards verified messages to Codex Desktop."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._common import write_json
from .feishu_sources import find_source_thread
from .feishu_pending import is_acknowledgement, is_question, message_time, own_bot, prior_messages, question_history


LOG = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
MESSAGE_ID = re.compile(r'^om_[A-Za-z0-9_-]+$')
CHAT_ID = re.compile(r'^oc_[A-Za-z0-9_-]+$')
USER_ID = re.compile(r'^ou_[A-Za-z0-9_-]+$')
THREAD_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
MAX_QUESTION_PAGES = 20
TYPES = {'text', 'post', 'image', 'file', 'audio', 'video'}
DEFAULT = {'enabled': False, 'app_id': '', 'bot_open_id': '', 'ack_text': '塔菲思考中，喵～',
           'reply_wait_seconds': 900, 'rules': []}


class UnrelatedGroupReply(RuntimeError):
    """A group reply chain has no message belonging to this bot or its jobs."""


class QuestionRoutingNeedsChoice(RuntimeError):
    """More than one waiting chat could receive an unthreaded group answer."""


def executable(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f'未找到 {name}，请安装并配置飞书 CLI。')
    return path


def lark_json(*args, cwd=None, timeout=35):
    result = subprocess.run([executable('lark-cli.cmd'), *args], cwd=cwd, capture_output=True,
                            text=True, encoding='utf-8', timeout=timeout, creationflags=NO_WINDOW)
    raw = result.stdout if result.returncode == 0 else result.stderr
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f'飞书 CLI 返回无效 JSON（退出码 {result.returncode}）') from exc
    if result.returncode or payload.get('ok') is not True:
        error = payload.get('error') or {}
        raise RuntimeError(f"飞书 CLI 失败：{error.get('message') or error.get('type') or result.returncode}")
    return payload


def verify_lark_app(config):
    result = subprocess.run([executable('lark-cli.cmd'), 'auth', 'status', '--json'],
                            capture_output=True, text=True, encoding='utf-8', timeout=15,
                            creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError('无法读取飞书 CLI 认证状态')
    payload = json.loads(result.stdout)
    if payload.get('appId') != config['app_id']:
        raise RuntimeError('监听配置的飞书 App ID 与当前 lark-cli 登录应用不一致')
    if payload.get('identities', {}).get('bot', {}).get('status') != 'ready':
        raise RuntimeError('飞书机器人身份未就绪')


def app_tools_server():
    root = Path.home() / '.codex' / 'plugins' / 'cache' / 'openai-bundled' / 'codex-app-tools'
    paths = sorted(root.glob('*/server.mjs'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        raise RuntimeError('未找到 Codex Desktop app tools。')
    return paths[0]


def app_pipes():
    current = os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '').strip()
    try:
        names = os.listdir(r'\\.\pipe')
    except OSError:
        names = []
    paths = [current] if current else []
    for name in names:
        if name.startswith('codex-browser-use-'):
            path = '\\\\.\\pipe\\' + name
            if path not in paths:
                paths.append(path)
    return paths


class AppTools:
    def __init__(self, pipe, thread_id):
        server = app_tools_server()
        env = os.environ.copy()
        env['CODEX_APP_TOOLS_PIPE_PATH'] = pipe
        self.thread_id = thread_id
        self.proc = subprocess.Popen([executable('node.exe'), str(server)], cwd=server.parent,
                                     env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1,
                                     creationflags=NO_WINDOW)
        self.messages = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._stdout, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        self.call('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                 'clientInfo': {'name': 'wintoolbox_feishu_bridge', 'version': '1.0'}}, 15)
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def _stdout(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                LOG.warning('Codex MCP 返回非 JSON 数据')
        self.messages.put({'_eof': True})

    def _stderr(self):
        for line in self.proc.stderr:
            LOG.debug('Codex MCP: %s', line.rstrip()[:300])

    def send(self, payload):
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def call(self, method, params, timeout=30):
        self.next_id += 1
        identifier = self.next_id
        self.send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'Codex MCP {method} 超时')
            answer = self.messages.get(timeout=remaining)
            if answer.get('_eof'):
                raise RuntimeError('Codex MCP 已断开')
            if answer.get('id') == identifier:
                if 'error' in answer:
                    raise RuntimeError(f"Codex MCP 错误：{answer['error']}")
                return answer.get('result') or {}

    def tool(self, name, arguments):
        result = self.call('tools/call', {'name': name, 'arguments': arguments,
                                         '_meta': {'openai/threadId': self.thread_id}})
        if result.get('isError'):
            detail = ' '.join(str(c.get('text', '')) for c in result.get('content', []) if c.get('type') == 'text')
            raise RuntimeError(f'Codex 工具调用失败：{detail[:600]}')
        return result

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()


def connect_codex(thread_id, launch=False):
    def attempt():
        failures = []
        for pipe in app_pipes():
            client = None
            try:
                client = AppTools(pipe, thread_id)
                names = {item.get('name') for item in client.call('tools/list', {}, 15).get('tools', [])}
                if 'send_message_to_thread' not in names:
                    raise RuntimeError('当前 Codex 桌面接口不支持向对话发送消息')
                return client
            except Exception as exc:
                failures.append(type(exc).__name__)
                if client is not None:
                    client.close()
        raise RuntimeError(f'无法连接 Codex 桌面接口（检查了 {len(failures)} 个管道）')
    try:
        return attempt()
    except RuntimeError:
        if not launch:
            raise
    subprocess.Popen([executable('codex.exe'), 'app'], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            return attempt()
        except RuntimeError:
            pass
    raise RuntimeError('Codex 桌面应用在 90 秒内未就绪')


def validate_rule(raw):
    if not isinstance(raw, dict):
        raise ValueError('飞书规则必须是对象')
    rule = {key: raw.get(key) for key in ('id', 'name', 'enabled', 'chat_type', 'chat_id',
             'sender_id', 'require_bot_mention', 'target_thread_id', 'prompt', 'message_types', 'ack_text', 'keyword')}
    rule['id'] = str(rule['id'] or uuid.uuid4().hex)
    rule['name'] = str(rule['name'] or '').strip()
    rule['chat_type'] = str(rule['chat_type'] or 'p2p')
    rule['chat_id'] = str(rule['chat_id'] or '').strip()
    rule['sender_id'] = str(rule['sender_id'] or '').strip()
    rule['target_thread_id'] = str(rule['target_thread_id'] or '').strip()
    rule['prompt'] = str(rule['prompt'] or '').strip()
    rule['ack_text'] = str(rule['ack_text'] or '').strip()
    rule['keyword'] = str(rule['keyword'] or '').strip()
    if not isinstance(raw.get('enabled', False), bool) or not isinstance(raw.get('require_bot_mention', False), bool):
        raise ValueError('规则开关必须为布尔值')
    rule['enabled'] = raw.get('enabled', False)
    rule['require_bot_mention'] = raw.get('require_bot_mention', False)
    hours = raw.get('pending_question_hours', 168)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 0 <= hours <= 720:
        raise ValueError('待答提问窗口应为 0–720 小时')
    rule['pending_question_hours'] = hours
    types = raw.get('message_types', ['text', 'post'])
    if not isinstance(types, list) or not types or any(item not in TYPES for item in types):
        raise ValueError('消息类型无效')
    rule['message_types'] = list(dict.fromkeys(types))
    if not rule['name'] or len(rule['name']) > 80:
        raise ValueError('请填写 1–80 字的规则名称')
    if rule['chat_type'] not in ('p2p', 'group') or not CHAT_ID.fullmatch(rule['chat_id']):
        raise ValueError('请选择私聊或群聊，并填写有效的 chat_id')
    if rule['sender_id'] and not USER_ID.fullmatch(rule['sender_id']):
        raise ValueError('发送者 open_id 无效')
    if rule['chat_type'] == 'p2p' and not rule['sender_id']:
        raise ValueError('私聊规则必须指定发送者 open_id')
    if not THREAD_ID.fullmatch(rule['target_thread_id']):
        raise ValueError('Codex 对话 ID 无效')
    if not rule['prompt'] or len(rule['prompt']) > 20000:
        raise ValueError('提示词应为 1–20000 字')
    if len(rule['ack_text']) > 200:
        raise ValueError('确认回复不能超过 200 字')
    if len(rule['keyword']) > 200:
        raise ValueError('关键词不能超过 200 字')
    return rule


def validate_config(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('enabled', False), bool):
        raise ValueError('飞书监听配置无效')
    value = {**DEFAULT, **raw}
    value['app_id'] = str(value['app_id'] or '').strip()
    value['bot_open_id'] = str(value['bot_open_id'] or '').strip()
    value['ack_text'] = str(value['ack_text'] or '').strip()
    wait = value['reply_wait_seconds']
    if not isinstance(wait, int) or isinstance(wait, bool) or not 30 <= wait <= 7200:
        raise ValueError('回复等待时间应为 30–7200 秒')
    if value['app_id'] and not re.fullmatch(r'cli_[A-Za-z0-9_-]+', value['app_id']):
        raise ValueError('飞书 App ID 无效')
    if value['bot_open_id'] and not USER_ID.fullmatch(value['bot_open_id']):
        raise ValueError('机器人 open_id 无效')
    if value['enabled'] and (not value['app_id'] or not value['bot_open_id']):
        raise ValueError('启用监听前请填写飞书 App ID 和机器人 open_id')
    if len(value['ack_text']) > 200:
        raise ValueError('确认回复不能超过 200 字')
    rules = value['rules']
    if not isinstance(rules, list) or len(rules) > 30:
        raise ValueError('最多支持 30 条飞书规则')
    value['rules'] = [validate_rule(item) for item in rules]
    if len({item['id'] for item in value['rules']}) != len(value['rules']):
        raise ValueError('飞书规则 ID 重复')
    if value['enabled'] and not any(item['enabled'] for item in value['rules']):
        raise ValueError('启用监听前请至少启用一条规则')
    return {key: value[key] for key in DEFAULT}


def matching_rule(event, config, *, allow_group_reply=False):
    if event.get('type') != 'im.message.receive_v1' or event.get('sender_type') != 'user':
        return None
    if not MESSAGE_ID.fullmatch(str(event.get('message_id', ''))):
        return None
    if event.get('sender_id') == config['bot_open_id']:
        return None
    for rule in config['rules']:
        if not rule['enabled'] or event.get('chat_type') != rule['chat_type'] or event.get('chat_id') != rule['chat_id']:
            continue
        if rule['sender_id'] and event.get('sender_id') != rule['sender_id']:
            continue
        if event.get('message_type') not in rule['message_types']:
            continue
        if rule['keyword'] and rule['keyword'].casefold() not in str(event.get('content', '')).casefold():
            continue
        if rule['require_bot_mention'] and not any(
                isinstance(item, dict) and item.get('id') == config['bot_open_id']
                for item in (event.get('mentions') or [])):
            parent = FeishuBridge._parent_id(event)
            if not (allow_group_reply and rule['chat_type'] == 'group'
                    and MESSAGE_ID.fullmatch(str(parent))):
                continue
        return rule
    return None


def render_prompt(rule, event, acknowledgement):
    replacements = {'{rule_name}': rule['name'], '{chat_id}': event['chat_id'],
                    '{message_id}': event['message_id'], '{sender_id}': event['sender_id'],
                    '{acknowledgement}': acknowledgement or '无'}
    prompt = rule['prompt']
    for marker, value in replacements.items():
        prompt = prompt.replace(marker, str(value))
    context = ''
    if event.get('context_source_message_id'):
        if event.get('context_route_kind') == 'pending_question':
            context = (f"本条群消息仅 @机器人，已按待答提问的来源继续当前 context。原提问消息 ID：{event['context_source_message_id']}。"
                       "先读取原提问，判断这条消息是否回答该问题；如果实际上是新话题，明确区分，不把它当成问题已解决。\n")
        else:
            context = (f"本条{'群聊' if rule['chat_type'] == 'group' else '私聊'}是回复消息，已按来源继续当前 context。被回复的消息 ID：{event['context_source_message_id']}。"
                       "读取该消息及必要的回复链，结合当前对话上下文理解请求。\n")
        if rule['chat_type'] == 'group':
            context += '这是群成员的项目消息，沿用群规则的处理范围；路由到来源对话不代表主人私聊授权。\n'
    return (
        f"飞书监听规则“{rule['name']}”已匹配原消息。chat_id={event['chat_id']}；"
        f"message_id={event['message_id']}；sender_id={event['sender_id']}。\n"
        "先用飞书 bot 身份重新读取原消息，核对 chat_id、sender.id 和 sender_type；"
        "仅在与以上值一致后处理消息。回复原消息时使用 --idempotency-key <message_id>:answer，"
        "检查 ok=true 和 data.message_id。不要重复发送思考中确认。\n\n"
        + context + prompt
    )


class FeishuBridge:
    def __init__(self, app):
        self.app = app
        self.path = app.data_dir / 'feishu-bridge.json'
        self.db_path = app.data_dir / 'feishu-bridge.sqlite3'
        self.lock = threading.RLock()
        self.config = self._load()
        self.connected = False
        self.last_error = ''
        self.last_event_at = None
        self.consumer = None
        self.consumer_thread = None
        self.consumer_stop = threading.Event()
        self.worker_stop = threading.Event()
        self.closed = False
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='feishu-message')
        self.db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (message_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, '
                        'status TEXT NOT NULL, reply_id TEXT, updated_at INTEGER NOT NULL, detail TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS message_contexts ('
                        'app_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id TEXT NOT NULL, '
                        'thread_id TEXT NOT NULL, source TEXT NOT NULL, created_at INTEGER NOT NULL, '
                        'PRIMARY KEY (app_id,chat_id,message_id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS pending_questions ('
                        'app_id TEXT NOT NULL,chat_id TEXT NOT NULL,message_id TEXT NOT NULL,'
                        'thread_id TEXT NOT NULL,created_at REAL NOT NULL,title TEXT NOT NULL,'
                        'targets TEXT NOT NULL,link TEXT NOT NULL,state TEXT NOT NULL,last_answer_id TEXT,'
                        'PRIMARY KEY (app_id,chat_id,message_id))')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        if 'target_thread_id' not in columns:
            self.db.execute('ALTER TABLE jobs ADD COLUMN target_thread_id TEXT')
        self.db.commit()
        if self.config['enabled']:
            self.start()

    def _load(self):
        if not self.path.exists():
            return validate_config(DEFAULT)
        return validate_config(json.loads(self.path.read_text('utf-8')))

    def status(self, _=None):
        with self.lock:
            rows = self.db.execute('SELECT message_id,rule_id,status,reply_id,updated_at,detail,target_thread_id '
                                   'FROM jobs ORDER BY updated_at DESC LIMIT 20').fetchall()
            return {'config': json.loads(json.dumps(self.config, ensure_ascii=False)),
                    'connected': self.connected, 'last_error': self.last_error,
                    'last_event_at': self.last_event_at,
                    'recent': [dict(zip(('message_id', 'rule_id', 'status', 'reply_id', 'updated_at', 'detail', 'target_thread_id'), row))
                               for row in rows]}

    def save(self, params):
        value = validate_config(params.get('config', params))
        with self.lock:
            was_enabled = self.config['enabled']
            write_json(self.path, value)
            self.config = value
        if was_enabled and not value['enabled']:
            self._stop_consumer()
        elif value['enabled'] and not self._running():
            self.start()
        return self.status()

    def threads(self, _=None):
        with self.lock:
            thread_id = next((rule['target_thread_id'] for rule in self.config['rules']), '')
        thread_id = thread_id or os.environ.get('CODEX_THREAD_ID', '')
        if not thread_id:
            raise ValueError('请先在规则中填写一个已有的 Codex 对话 ID，再读取对话列表')
        client = connect_codex(thread_id)
        try:
            result = client.tool('list_threads', {'limit': 50})
        finally:
            client.close()
        block = next((item.get('text') for item in result.get('content', []) if item.get('type') == 'text'), '')
        payload = json.loads(block)
        seen = set()
        rows = []
        for item in payload.get('pinnedThreads', []) + payload.get('threads', []):
            if item.get('kind') != 'codex' or item.get('hostId') != 'local' or item.get('id') in seen:
                continue
            seen.add(item['id'])
            rows.append({'id': item['id'], 'title': item.get('title') or item['id'], 'cwd': item.get('cwd') or ''})
        return {'threads': rows}

    def check(self, _=None):
        executable('lark-cli.cmd')
        lark_json('event', 'consume', 'im.message.receive_v1', '--as', 'bot', '--dry-run')
        with self.lock:
            config = dict(self.config)
            threads = list({rule['target_thread_id'] for rule in self.config['rules'] if rule['enabled']})
        if config['app_id']:
            verify_lark_app(config)
        if threads:
            client = connect_codex(threads[0])
            client.close()
        return {'ok': True, 'lark': 'ready', 'codex': 'ready' if threads else 'no_enabled_rule'}

    def _running(self):
        return self.consumer_thread is not None and self.consumer_thread.is_alive()

    def start(self):
        if self._running():
            return
        self.consumer_stop = threading.Event()
        self.consumer_thread = threading.Thread(target=self._consume_loop, args=(self.consumer_stop,),
                                                name='feishu-bridge-consumer', daemon=True)
        self.consumer_thread.start()

    def _stop_consumer(self):
        self.consumer_stop.set()
        proc = self.consumer
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                proc.terminate()
        if self.consumer_thread and self.consumer_thread is not threading.current_thread():
            self.consumer_thread.join(timeout=5)
        self.connected = False

    def _consume_loop(self, stop):
        while not stop.is_set():
            try:
                with self.lock:
                    config = dict(self.config)
                verify_lark_app(config)
                proc = subprocess.Popen([executable('lark-cli.cmd'), 'event', 'consume',
                                         'im.message.receive_v1', '--as', 'bot'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', bufsize=1, creationflags=NO_WINDOW)
                with self.lock:
                    self.consumer = proc
                ready = threading.Event()

                def stderr_reader():
                    for line in proc.stderr:
                        line = line.rstrip()
                        if '[event] ready event_key=im.message.receive_v1' in line:
                            ready.set()
                            with self.lock:
                                self.connected = True
                                self.last_error = ''
                            self.app.emit('feishu.bridge.status', connected=True)
                        elif line and ('WARN' in line or 'ERROR' in line or 'error' in line.lower()):
                            LOG.warning('飞书监听：%s', line[:500])
                threading.Thread(target=stderr_reader, daemon=True).start()
                for line in proc.stdout:
                    if stop.is_set():
                        break
                    try:
                        event = json.loads(line)
                    except ValueError:
                        LOG.warning('飞书事件不是 JSON')
                        continue
                    if ready.is_set():
                        self._accept(event)
                if stop.is_set():
                    break
                raise RuntimeError(f'飞书监听进程已退出（{proc.wait()}）')
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.last_error = str(exc)
                LOG.exception('飞书监听中断')
                self.app.emit('feishu.bridge.status', connected=False, error=str(exc))
                stop.wait(5)
        with self.lock:
            self.connected = False
            self.consumer = None

    def _claim(self, message_id, rule_id):
        with self.lock:
            try:
                self.db.execute('INSERT INTO jobs (message_id,rule_id,status,reply_id,updated_at,detail) VALUES (?,?,?,NULL,?,NULL)',
                                (message_id, rule_id, 'processing', int(time.time())))
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def _finish(self, message_id, status, reply_id='', detail=''):
        with self.lock:
            self.db.execute('UPDATE jobs SET status=?,reply_id=?,updated_at=?,detail=? WHERE message_id=?',
                            (status, reply_id, int(time.time()), detail[:1000], message_id))
            self.db.commit()
        self.app.emit('feishu.bridge.job', message_id=message_id, status=status, reply_id=reply_id)

    def _accept(self, event):
        with self.lock:
            rule = matching_rule(event, self.config, allow_group_reply=True)
            if not rule or not self._claim(event['message_id'], rule['id']):
                return
            rule = dict(rule)
            config = dict(self.config)
            self.last_event_at = int(time.time())
        implicit = rule['chat_type'] == 'group' and rule['require_bot_mention'] and not any(
            isinstance(item, dict) and item.get('id') == config['bot_open_id']
            for item in (event.get('mentions') or []))
        self.workers.submit(self._process_safe, {**event, '_implicit_group_reply': implicit}, rule, config, '')

    def _process_safe(self, event, rule, config, ack_id):
        try:
            self._process(event, rule, config, ack_id)
        except Exception as exc:
            LOG.exception('飞书消息处理失败：%s', event.get('message_id'))
            self._finish(event['message_id'], 'failed', detail=str(exc))

    def _remember_context(self, message_id, rule, config, thread_id, source):
        if not MESSAGE_ID.fullmatch(message_id) or not THREAD_ID.fullmatch(thread_id):
            raise RuntimeError('消息来源映射的 ID 无效')
        with self.lock:
            row = self.db.execute('SELECT thread_id,source FROM message_contexts WHERE app_id=? AND chat_id=? AND message_id=?',
                                  (config['app_id'], rule['chat_id'], message_id)).fetchone()
            legacy_group = rule['chat_type'] == 'group' and row and not row[1].startswith('group:')
            if row and row[0] != thread_id and not legacy_group:
                raise RuntimeError('原消息的 context 映射冲突')
            if rule['chat_type'] == 'group':
                source = 'group:' + source
            if legacy_group:
                # Older group dispatches always used the default, including
                # replies. Replace only after provenance has been reverified.
                self.db.execute('UPDATE message_contexts SET thread_id=?,source=?,created_at=? '
                                'WHERE app_id=? AND chat_id=? AND message_id=?',
                                (thread_id, source, int(time.time()), config['app_id'], rule['chat_id'], message_id))
            self.db.execute('INSERT OR IGNORE INTO message_contexts VALUES (?,?,?,?,?,?)',
                            (config['app_id'], rule['chat_id'], message_id, thread_id, source, int(time.time())))
            self.db.commit()

    def _known_context(self, message_id, rule, config):
        with self.lock:
            row = self.db.execute('SELECT thread_id,source FROM message_contexts WHERE app_id=? AND chat_id=? AND message_id=?',
                                  (config['app_id'], rule['chat_id'], message_id)).fetchone()
        if row and rule['chat_type'] == 'group' and not row[1].startswith('group:'):
            return None
        return row[0] if row else None

    @staticmethod
    def _parent_id(message):
        return message.get('reply_to') or message.get('parent_id') or message.get('root_id') or ''

    def _pending_question_context(self, message, rule, config):
        hours = rule.get('pending_question_hours', 168)
        if not hours:
            return None
        cutoff = time.time() - hours * 3600
        history = []
        token = ''
        seen_tokens = set()
        complete = False
        for _ in range(MAX_QUESTION_PAGES):
            args = ['im', '+chat-messages-list', '--as', 'bot', '--chat-id', rule['chat_id'],
                    '--order', 'desc', '--page-size', '50', '--no-reactions', '--format', 'json']
            if token:
                args += ['--page-token', token]
            answer = lark_json(*args)
            batch = answer.get('data', {}).get('messages', [])
            history.extend(batch)
            token = answer.get('data', {}).get('page_token') or ''
            timestamps = [message_time(m) for m in batch if message_time(m)]
            if not token or not batch or (timestamps and min(timestamps) < cutoff):
                complete = True
                break
            if token in seen_tokens:
                break
            seen_tokens.add(token)
        if not complete:
            raise QuestionRoutingNeedsChoice('追问窗口内的群历史未能完整读取，不能安全确定唯一来源。请用飞书“回复”对应提问，或缩短追问窗口。')
        questions, answered = question_history(history, rule, config, message, cutoff)
        sender_id = (message.get('sender') or {}).get('id')
        for mid, question in questions.items():
            if not MESSAGE_ID.fullmatch(mid):
                continue
            targets = [m.get('id') for m in (question.get('mentions') or [])
                       if isinstance(m, dict) and USER_ID.fullmatch(str(m.get('id', '')))
                       and m.get('id') != config['bot_open_id']]
            source = self._known_context(mid, rule, config) or ''
            if mid not in answered and not source:
                source = find_source_thread(question) or ''
            if source:
                self._remember_context(mid, rule, config, source, 'pending_question')
            with self.lock:
                self.db.execute('INSERT INTO pending_questions VALUES (?,?,?,?,?,?,?,?,?,NULL) '
                                'ON CONFLICT(app_id,chat_id,message_id) DO UPDATE SET '
                                "thread_id=CASE WHEN excluded.thread_id<>'' THEN excluded.thread_id ELSE thread_id END,"
                                'title=excluded.title,targets=excluded.targets,link=excluded.link,'
                                "state=CASE WHEN excluded.state='answered' THEN 'answered' ELSE state END",
                                (config['app_id'], rule['chat_id'], mid, source, message_time(question),
                                 str(question.get('content', '')).replace('\\n', ' ').replace('\n', ' ')[:160],
                                 json.dumps(targets), question.get('message_app_link') or '',
                                 'answered' if mid in answered else 'open'))
                self.db.commit()
        # Recover completion after a toolbox restart, including answers sent
        # with @ rather than Feishu's explicit reply relationship.
        with self.lock:
            for followup in prior_messages(history, rule, message):
                if own_bot(followup, config) and not is_acknowledgement(followup, rule, config):
                    self.db.execute("UPDATE pending_questions SET state='answered' WHERE app_id=? AND chat_id=? AND last_answer_id=?",
                                    (config['app_id'], rule['chat_id'], self._parent_id(followup)))
            self.db.commit()
        with self.lock:
            rows = self.db.execute('SELECT message_id,thread_id,title,targets,link FROM pending_questions '
                                   "WHERE app_id=? AND chat_id=? AND state='open' AND created_at>=? "
                                   'ORDER BY created_at DESC', (config['app_id'], rule['chat_id'], cutoff)).fetchall()
        targeted = [row for row in rows if sender_id in json.loads(row[3])]
        general = [row for row in rows if not json.loads(row[3])]
        choices = targeted or general or rows
        if not choices:
            return None
        if any(not row[1] for row in choices):
            raise RuntimeError('群里有待答提问，但无法核验其来源 Codex 对话；请回复对应原问题，不会误送默认对话')
        if len({row[1] for row in choices}) != 1:
            summaries = '\n'.join(f'{i}. {row[2][:80]}\n{row[4]}' for i, row in enumerate(choices[:5], 1))
            raise QuestionRoutingNeedsChoice('群里有来自不同 Codex 对话的待答提问，请用飞书“回复”对应问题：\n' + summaries)
        selected = choices[0]
        # Cached questions absent from current history still need a
        # current same-chat read before they can select a destination.
        if selected[0] not in questions:
            current = lark_json('im', '+messages-mget', '--as', 'bot', '--message-ids', selected[0],
                                '--no-reactions', '--format', 'json')
            original = next((m for m in current.get('data', {}).get('messages', [])
                             if m.get('message_id') == selected[0]), None)
            if not original or original.get('deleted') or original.get('chat_id') != rule['chat_id'] or not own_bot(original, config) or not is_question(original):
                with self.lock:
                    self.db.execute("UPDATE pending_questions SET state='answered' WHERE app_id=? AND chat_id=? AND message_id=?",
                                    (config['app_id'], rule['chat_id'], selected[0]))
                    self.db.commit()
                raise RuntimeError('原待答提问已删除或改变，请重发消息或回复对应原问题')
        return selected[1], selected[0]

    def _track_question_answer(self, question_id, answer_id, rule, config, *, completed=False):
        if not question_id:
            return
        with self.lock:
            if completed:
                self.db.execute("UPDATE pending_questions SET state='answered' WHERE app_id=? AND chat_id=? AND message_id=? AND last_answer_id=?",
                                (config['app_id'], rule['chat_id'], question_id, answer_id))
            else:
                self.db.execute("UPDATE pending_questions SET last_answer_id=? WHERE app_id=? AND chat_id=? AND message_id=? AND state='open'",
                                (answer_id, config['app_id'], rule['chat_id'], question_id))
            self.db.commit()

    def _resolve_context(self, message, rule, config):
        parent_id = self._parent_id(message)
        if not parent_id:
            if rule['chat_type'] == 'group' and any(isinstance(m, dict) and m.get('id') == config['bot_open_id'] for m in (message.get('mentions') or [])):
                pending = self._pending_question_context(message, rule, config)
                if pending:
                    return pending
            return rule['target_thread_id'], ''
        original_parent = parent_id
        visited = set()
        ancestors = []
        bot_messages = []
        while parent_id and parent_id not in visited and len(visited) < 20:
            if not MESSAGE_ID.fullmatch(parent_id):
                raise RuntimeError('被回复的消息 ID 无效')
            visited.add(parent_id)
            known = self._known_context(parent_id, rule, config)
            if known:
                for ancestor in ancestors:
                    self._remember_context(ancestor, rule, config, known, 'reply_chain')
                return known, original_parent
            answer = lark_json('im', '+messages-mget', '--as', 'bot', '--message-ids', parent_id,
                               '--no-reactions', '--format', 'json')
            parent = next((item for item in answer.get('data', {}).get('messages', [])
                           if item.get('message_id') == parent_id), None)
            if not parent or parent.get('chat_id') != rule['chat_id'] or parent.get('deleted'):
                raise RuntimeError('无法读取同一会话内被回复的原消息')
            sender = parent.get('sender') or {}
            own_bot = sender.get('sender_type') in ('app', 'bot') and sender.get('id') in (config['app_id'], config['bot_open_id'])
            owner = sender.get('sender_type') == 'user' and sender.get('id') == rule['sender_id']
            group_member = rule['chat_type'] == 'group' and sender.get('sender_type') == 'user' and USER_ID.fullmatch(str(sender.get('id', '')))
            if not own_bot and not owner and not group_member:
                if rule['chat_type'] == 'group' and sender.get('sender_type') in ('app', 'bot'):
                    raise UnrelatedGroupReply('被回复的是其他机器人消息')
                raise RuntimeError('原消息发送者不属于当前会话的可识别用户或机器人')
            ancestors.append(parent_id)
            # A group question may be sent by another Codex chat under an older
            # discussion. Its own send receipt wins over that discussion's map.
            if own_bot and rule['chat_type'] == 'group':
                source = find_source_thread(parent)
                if not source:
                    raise RuntimeError('找不到被回复机器人消息的来源 context；不会派发到默认 context')
                for ancestor in ancestors:
                    self._remember_context(ancestor, rule, config, source, 'codex_receipt')
                return source, original_parent
            # A reply chain is authoritative even when the final response was sent
            # by a delegated agent. Recover standalone notification provenance only.
            next_parent = self._parent_id(parent)
            if own_bot:
                bot_messages.append(parent)
            parent_id = next_parent
        if parent_id:
            raise RuntimeError('原消息回复链存在循环或超过 20 层，无法确定 context')
        if rule['chat_type'] == 'group' and not bot_messages:
            raise UnrelatedGroupReply('群回复链没有机器人消息或已知 context')
        for parent in bot_messages:
            source = find_source_thread(parent)
            if source:
                for ancestor in ancestors:
                    self._remember_context(ancestor, rule, config, source, 'codex_receipt')
                return source, original_parent
        raise RuntimeError('找不到被回复消息的来源 context；请直接发送新消息使用默认 context')

    def _process(self, event, rule, config, ack_id=''):
        message_id = event['message_id']
        try:
            answer = lark_json('im', '+messages-mget', '--as', 'bot', '--message-ids', message_id,
                               '--no-reactions', '--format', 'json')
            message = next((m for m in answer.get('data', {}).get('messages', [])
                            if m.get('message_id') == message_id), None)
            sender = (message or {}).get('sender') or {}
            if not message or message.get('chat_id') != rule['chat_id'] or sender.get('id') != event['sender_id'] or sender.get('sender_type') != 'user':
                raise RuntimeError('原消息的会话或发送者与飞书事件不一致')
            if not str(message.get('content', '')).strip():
                raise RuntimeError('原消息内容为空')
            if self._parent_id(event) and not self._parent_id(message):
                raise RuntimeError('无法从原消息复核回复关系，暂不派发到默认 context')
            target_thread_id, source_message_id = self._resolve_context(message, rule, config)
            event = {**event, 'context_source_message_id': source_message_id,
                     'context_route_kind': 'reply' if self._parent_id(message) else 'pending_question' if source_message_id else 'default'}
            self._remember_context(message_id, rule, config, target_thread_id, 'dispatch')
            ack = rule['ack_text'] or config['ack_text']
            if ack and not ack_id:
                try:
                    acknowledgement = lark_json('im', '+messages-reply', '--as', 'bot', '--message-id', message_id,
                                               '--text', ack, '--idempotency-key', message_id + ':thinking', '--format', 'json')
                    ack_id = acknowledgement.get('data', {}).get('message_id', '')
                except Exception:
                    LOG.exception('飞书确认回复失败，继续派发 %s', message_id)
            if ack_id:
                self._remember_context(ack_id, rule, config, target_thread_id, 'acknowledgement')
            with self.lock:
                self.db.execute('UPDATE jobs SET target_thread_id=? WHERE message_id=?', (target_thread_id, message_id))
                self.db.commit()
            prompt = render_prompt(rule, event, ack_id)
            client = connect_codex(target_thread_id, launch=True)
            try:
                client.tool('send_message_to_thread', {'threadId': target_thread_id,
                                                       'hostId': 'local', 'prompt': prompt})
            finally:
                client.close()
            self._finish(message_id, 'dispatched')
            self._track_question_answer(source_message_id, message_id, rule, config)
        except QuestionRoutingNeedsChoice as exc:
            self._finish(message_id, 'needs_context', detail=str(exc))
            try:
                sent = lark_json('im', '+messages-reply', '--as', 'bot', '--message-id', message_id,
                                 '--text', '喵，' + str(exc), '--idempotency-key', message_id + ':context-choice', '--format', 'json')
                reply_id = sent.get('data', {}).get('message_id', '')
                if not MESSAGE_ID.fullmatch(reply_id):
                    raise RuntimeError('来源选择提示没有返回有效消息 ID')
                self._finish(message_id, 'needs_context', reply_id, detail=str(exc))
            except Exception:
                LOG.exception('飞书来源选择提示未能送达：%s', message_id)
            return
        except UnrelatedGroupReply as exc:
            if event.get('_implicit_group_reply'):
                self._finish(message_id, 'ignored', detail=str(exc))
                return
            self._finish(message_id, 'failed', detail=str(exc))
            self._blocked(message_id, str(exc))
            return
        except Exception as exc:
            self._finish(message_id, 'failed', detail=str(exc))
            self._blocked(message_id, str(exc))
            return
        deadline = time.monotonic() + config['reply_wait_seconds']
        while time.monotonic() < deadline and not self.worker_stop.is_set():
            try:
                answer = lark_json('im', '+chat-messages-list', '--as', 'bot', '--chat-id', rule['chat_id'],
                                   '--order', 'desc', '--page-size', '50', '--no-reactions', '--format', 'json')
                for item in answer.get('data', {}).get('messages', []):
                    sender = item.get('sender') or {}
                    if item.get('reply_to') == message_id and sender.get('id') == config['app_id'] and item.get('message_id') != ack_id:
                        self._remember_context(item['message_id'], rule, config, target_thread_id, 'final_reply')
                        self._track_question_answer(source_message_id, message_id, rule, config, completed=True)
                        self._finish(message_id, 'sent', item['message_id'])
                        return
            except Exception as exc:
                self._finish(message_id, 'unconfirmed', detail=str(exc))
                return
            self.worker_stop.wait(5)
        self._finish(message_id, 'unconfirmed', detail='等待最终回复超时或工具箱正在退出')

    def _blocked(self, message_id, reason):
        try:
            lark_json('im', '+messages-reply', '--as', 'bot', '--message-id', message_id,
                      '--markdown', f'喵，连接 Codex 时遇到阻塞：{reason[:300]}。请在工具箱的飞书监听设置中检查状态。',
                      '--idempotency-key', message_id + ':blocked', '--format', 'json')
        except Exception:
            LOG.exception('飞书阻塞提示未能送达：%s', message_id)

    def close(self):
        if self.closed:
            return
        self.closed = True
        with self.lock:
            self.worker_stop.set()
        self._stop_consumer()
        self.workers.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.db.close()


def register(app):
    service = FeishuBridge(app)
    app.feishu_bridge = service
    app.register('feishu.bridge.status', service.status)
    app.register('feishu.bridge.save', service.save)
    app.register('feishu.bridge.check', service.check)
    app.register('feishu.bridge.threads', service.threads)
