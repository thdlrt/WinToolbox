"""Desk companion data service. Routine sampling never starts a model conversation."""
import copy
import http.client
import ipaddress
import json
import logging
import math
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request

from ..settings import atomic_json, protect
from .desktop_metrics import Metrics
from .bambu_monitor import Printer, discover, studio_printer
from .stackchan_panel import PAGES, SPEEDS, panel_packet, recent_tasks
from .stackchan_interaction import Interactions, notice_transport_slot
from .stackchan_attention import WaitingNotices
from .stackchan_health import RobotHealth
from .stackchan_sessions import AgentSessions

LOG = logging.getLogger(__name__)
DEFAULTS = {'enabled': False, 'host': '', 'port': 8080, 'city': '', 'latitude': None,
            'longitude': None, 'gpu': '', 'thread_id': '', 'default_page': 'tasks',
            'rotation_enabled': True, 'rotation_speed': 'normal',
            'rotation_pages': ['tasks', 'weather', 'pc', 'printer', 'quota'],
            'printer_enabled': False, 'printer_connection': 'lan', 'printer_host': '', 'printer_serial': '', 'printer_name': 'Bambu P2S',
            'interaction_enabled': True, 'pet_enabled': True, 'search_enabled': True, 'welcome_enabled': True, 'summary_ai': True,
            'away_minutes': 5, 'motion_seconds': 3.5, 'camera_mirror': False, 'companion_dir': ''}
DEFAULTS.update(presence_enabled=False, screen_mode='auto', auto_screen_off=True, presence_absence_seconds=60)
DEFAULTS.update(dsh_enabled=True, dsh_port=3081)
DEFAULTS.update(search_cooldown_seconds=300)


def normalize_quota(raw):
    buckets = raw.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict):
        old = raw.get('rateLimits')
        buckets = {old.get('limitId') or 'codex': old} if isinstance(old, dict) else {}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for window in ('primary', 'secondary'):
            value = bucket.get(window)
            if not isinstance(value, dict):
                continue
            used, duration, reset = (value.get(k) for k in ('usedPercent', 'windowDurationMins', 'resetsAt'))
            if not isinstance(used, (int, float)) or not math.isfinite(used):
                continue
            windows.append({'bucket': str(key)[:40], 'window': window,
                            'minutes': duration if isinstance(duration, (int, float)) and duration > 0 else None,
                            'remaining': round(max(0, min(100, 100 - used)), 1),
                            'resets_at': reset if isinstance(reset, (int, float)) and math.isfinite(reset) else None})
    return {'windows': windows[:12], 'available': bool(windows), 'sampled_at': time.time()}


def read_quota(thread_id):
    from .feishu_bridge import connect_codex
    client = connect_codex(thread_id, launch=False)
    try:
        names = {item.get('name') for item in client.call('tools/list', {}).get('tools', [])}
        if 'get_usage_limits' not in names:
            raise RuntimeError('当前 Codex Desktop 没有只读额度接口，请更新 Codex')
        result = client.tool('get_usage_limits', {})
        raw = result.get('structuredContent')
        if not isinstance(raw, dict):
            raw = next((json.loads(block['text']) for block in result.get('content', []) if block.get('type') == 'text'), {})
        value = normalize_quota(raw)
        if not value['available']:
            raise RuntimeError('Codex 尚未返回额度；请检查当前账号登录状态')
        return value
    finally:
        client.close()


def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'WinToolbox-StackChan/1.0'})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = response.read(256001)
    if len(data) > 256000:
        raise ValueError('天气服务响应过大')
    return json.loads(data)


def cities(query):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 80:
        raise ValueError('请输入城市名称')
    data = get_json('https://geocoding-api.open-meteo.com/v1/search?' + urllib.parse.urlencode(
        {'name': query.strip(), 'count': 8, 'language': 'zh', 'format': 'json'}))
    return [{'name': ', '.join(filter(None, [row.get('name'), row.get('admin1'), row.get('country')])),
             'latitude': row['latitude'], 'longitude': row['longitude']} for row in data.get('results', [])]


def weather(settings):
    if settings['latitude'] is None or settings['longitude'] is None:
        return {'available': False, 'error': '请在电脑设置天气城市'}
    data = get_json('https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode({
        'latitude': settings['latitude'], 'longitude': settings['longitude'], 'timezone': 'auto',
        'current': 'temperature_2m,weather_code,is_day',
        'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max', 'forecast_days': 1}))
    current, daily = data['current'], data['daily']
    return {'available': True, 'temperature': current['temperature_2m'], 'code': current['weather_code'],
            'day': bool(current['is_day']), 'high': daily['temperature_2m_max'][0],
            'low': daily['temperature_2m_min'][0], 'rain': daily['precipitation_probability_max'][0],
            'sampled_at': time.time(), 'city': settings['city']}


def safe_host(value):
    if not isinstance(value, str):
        raise ValueError('设备地址必须是局域网 IP')
    address = ipaddress.ip_address(value.strip())
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ValueError('请填写机器人所在的局域网 IPv4 地址')
    return str(address)


class DeskService:
    def __init__(self, app, *, metrics=None, quota_reader=read_quota, weather_reader=weather,
                 session_reader=None, printer=None, sender=None):
        self.app = app
        self.path = Path(app.data_dir) / 'stackchan.local.json'
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.metrics = metrics or Metrics()
        self.quota_reader, self.weather_reader = quota_reader, weather_reader
        self.session_reader = session_reader
        self.agent_sessions = AgentSessions()
        self.printer = printer or Printer(data_dir=app.data_dir)
        self.sender = sender or self.send
        self.thread = None
        self.closed = False
        self.config = {**DEFAULTS, **self._load()}
        self.data = {'connected': False, 'page': 'face', 'error': None, 'metrics': {}, 'weather': {}, 'quota': {},
                     'sessions': {}, 'printer': {}, 'printer_import': None, 'ai_quota': {}}
        self.candidates = []
        self.city_query = None
        self.force = True
        self.revision = 0
        self.last_metric = self.last_weather = self.last_quota = self.last_sessions = 0
        self.last_ai_list = self.last_ai_refresh = 0
        self.last_printer_diagnostic = 0
        self.refresh_seq = None
        self.quota_step = None
        self.group_page = None
        self.group_since = 0
        self.group_cursors = {'tasks': -1, 'quota': -1}
        self.slow_busy = set()
        self.retry_at = {}
        self.interactions = Interactions(self)
        self.attention = WaitingNotices(app.data_dir, lambda: not self.closed and self.config['enabled'])
        self.health = RobotHealth(app.data_dir)

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text('utf-8'))
            return {k: v for k, v in value.items() if k in {*DEFAULTS, 'token_protected', 'printer_access_protected'}}
        except (OSError, ValueError):
            LOG.exception('无法读取 StackChan 本机设置')
            return {}

    def status(self, _=None):
        with self.lock:
            config = {k: v for k, v in self.config.items() if k not in ('token_protected', 'printer_access_protected')}
            return {'settings': {**config, 'has_token': bool(self.config.get('token_protected')),
                                 'has_printer_access_code': bool(self.config.get('printer_access_protected'))},
                    **copy.deepcopy(self.data), 'cities': copy.deepcopy(self.candidates),
                    'running': bool(self.thread and self.thread.is_alive()), 'interaction': self.interactions.status(),
                    'attention': self.attention.status(), 'health': self.health.snapshot()}

    def _send_panel(self, config, packet):
        with notice_transport_slot(config) as available:
            if not available:
                return None
            # Read urgent state before decoding another atlas on the ESP32.
            state = self.sender(config, {'action': 'state'})
            interaction = (state or {}).get('interaction', {})
            pending = interaction.get('pending') or {}
            quiet_finding = pending.get('kind') in ('presence', 'pet') and not interaction.get('busy')
            # A presence window can last a minute. Actual photos share this DB lock,
            # so quiet observation can safely receive fresh telemetry between frames.
            capture = self.interactions.capture_active and not quiet_finding
            reaction = pending and not quiet_finding
            if capture or reaction or interaction.get('busy') or (state or {}).get('display', {}).get('off'):
                return state
            return self.sender(config, packet)

    def save(self, params):
        if set(params) - {*DEFAULTS, 'token', 'printer_access_code'}:
            raise ValueError('机器人设置包含未知字段')
        with self.lock:
            value = {**self.config, **{k: v for k, v in params.items() if k not in ('token', 'printer_access_code')}}
            if type(value['enabled']) is not bool:
                raise ValueError('启用状态无效')
            if value['host']:
                value['host'] = safe_host(value['host'])
            if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
                raise ValueError('端口必须为 1–65535')
            for key in ('interaction_enabled', 'pet_enabled', 'search_enabled', 'welcome_enabled', 'summary_ai', 'camera_mirror',
                        'presence_enabled', 'auto_screen_off', 'dsh_enabled'):
                if type(value[key]) is not bool:
                    raise ValueError('互动设置无效')
            if type(value['dsh_port']) is not int or not 1 <= value['dsh_port'] <= 65535:
                raise ValueError('DSH 本机入口端口无效')
            if type(value['presence_absence_seconds']) is not int or value['presence_absence_seconds'] not in (30, 60, 120, 300):
                raise ValueError('离席确认时间需为30、60、120或300秒')
            if value['screen_mode'] not in ('auto', 'on', 'off'):
                raise ValueError('屏幕模式无效')
            if type(value['away_minutes']) is not int or value['away_minutes'] not in (2, 5, 10, 15):
                raise ValueError('离席时间需为2、5、10或15分钟')
            if type(value['motion_seconds']) not in (int, float) or value['motion_seconds'] not in (2, 3, 3.5, 5):
                raise ValueError('动作时间需为2、3、3.5或5秒')
            if type(value['search_cooldown_seconds']) is not int or value['search_cooldown_seconds'] not in (180, 300, 600):
                raise ValueError('自动找人间隔需为3、5或10分钟')
            if not isinstance(value['companion_dir'], str) or len(value['companion_dir']) > 500:
                raise ValueError('提醒记录目录无效')
            if value['default_page'] not in PAGES:
                raise ValueError('默认页面无效')
            if type(value['rotation_enabled']) is not bool or value['rotation_speed'] not in SPEEDS:
                raise ValueError('轮播速度无效')
            if not isinstance(value['rotation_pages'], list) or not 1 <= len(value['rotation_pages']) <= len(PAGES) or \
                    any(p not in PAGES for p in value['rotation_pages']) or len(set(value['rotation_pages'])) != len(value['rotation_pages']):
                raise ValueError('请选择至少一个轮播页面，且不能重复')
            if type(value['printer_enabled']) is not bool:
                raise ValueError('打印机启用状态无效')
            if value['printer_connection'] not in ('lan', 'cloud'):
                raise ValueError('打印机连接方式无效')
            if value['printer_host']:
                from .bambu_monitor import private_host
                value['printer_host'] = private_host(value['printer_host'].strip())
            for key, limit in [('printer_serial', 40), ('printer_name', 40)]:
                if not isinstance(value[key], str) or len(value[key]) > limit:
                    raise ValueError('打印机名称或序列号无效')
            if value['printer_serial'] and not value['printer_serial'].isalnum():
                raise ValueError('打印机序列号只允许字母和数字')
            if params.get('printer_access_code'):
                code = params['printer_access_code']
                if not isinstance(code, str) or len(code) != 8 or not code.isascii() or not code.isalnum():
                    raise ValueError('拓竹访问码需为8位字母或数字')
                value['printer_access_protected'] = protect(code)
            if value['printer_enabled'] and value['printer_connection'] == 'cloud' and not value['printer_serial']:
                raise ValueError('请先从 Bambu Studio 导入打印机序列号')
            if value['printer_enabled'] and value['printer_connection'] == 'lan' and not all(value.get(k) for k in ('printer_host', 'printer_serial', 'printer_access_protected')):
                raise ValueError('请先导入打印机配置并填写可访问的 IP')
            for key, limit in [('city', 120), ('gpu', 100), ('thread_id', 100)]:
                if not isinstance(value[key], str) or len(value[key]) > limit:
                    raise ValueError(f'{key} 无效')
            for key, limit in [('latitude', 90), ('longitude', 180)]:
                item = value[key]
                if item is not None and (type(item) not in (int, float) or not math.isfinite(item) or abs(item) > limit):
                    raise ValueError('城市坐标无效')
            if (value['latitude'] is None) != (value['longitude'] is None):
                raise ValueError('请选择完整的城市坐标')
            if params.get('token'):
                token = params['token']
                if not isinstance(token, str) or not 16 <= len(token.strip()) <= 256:
                    raise ValueError('设备令牌需为 16–256 字符')
                value['token_protected'] = protect(token.strip())
            if value['enabled'] and (not value['host'] or not value.get('token_protected')):
                raise ValueError('请先填写机器人地址和设备令牌')
            atomic_json(self.path, value)
            self.config = value
            self.revision += 1
            self.force = True
            self.last_weather = self.last_quota = 0
            self.retry_at.clear()
        self.start()
        self.wake.set()
        return self.status()

    def import_printer(self, params=None):
        connection = (params or {}).get('connection', self.config['printer_connection'])
        if connection not in ('lan', 'cloud'):
            raise ValueError('打印机连接方式无效')
        with self.lock:
            if self.data.get('printer_import') == 'running':
                return self.status()
            self.data['printer_import'] = 'running'
        def run():
            try:
                cloud = connection == 'cloud'
                imported = studio_printer(require_access=not cloud)
                imported['printer_connection'] = connection
                hosts = [] if cloud else discover(imported['printer_serial'])
                with self.lock:
                    if self.closed:
                        return
                    if hosts and not self.config['printer_host']:
                        imported.update(printer_host=hosts[0], printer_enabled=True)
                    # Importing while offline is useful: preserve the credential securely,
                    # and let the owner enter a routed/VPN address later.
                    self.save(imported)
                    if cloud:
                        self.printer.key = None  # Reload an updated Studio login on the worker's next configure.
                    self.data['printer_import'] = 'done' if cloud or self.config['printer_host'] else 'need_address'
            except Exception as exc:
                with self.lock:
                    self.data['printer_import'] = f'导入失败：{type(exc).__name__}，请检查 Bambu Studio 或手动填写'
        threading.Thread(target=run, name='stackchan-printer-import', daemon=True).start()
        return self.status()

    def refresh(self, _=None):
        with self.lock:
            self.force = True
        self.wake.set()
        return self.status()

    def search(self, params):
        query = params.get('query', '')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 80:
            raise ValueError('请输入城市名称')
        with self.lock:
            self.city_query = query.strip()
            self.candidates = []
            self.data['city_search'] = 'searching'
        self.start()
        self.wake.set()
        return self.status()

    def send(self, settings, packet):
        token = protect(settings['token_protected'], decrypt=True)
        connection = http.client.HTTPConnection(safe_host(settings['host']), settings['port'], timeout=6)
        try:
            body = json.dumps(packet, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if len(body) > 7000:
                raise ValueError('机器人数据超过安全大小')
            connection.request('POST', '/desktop', body, {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
            response = connection.getresponse()
            data = response.read(8193)
            if response.status != 200:
                raise RuntimeError(f'机器人连接失败：HTTP {response.status}，请检查固件和令牌')
            if len(data) > 8192:
                raise RuntimeError('机器人响应过大')
            return json.loads(data)
        finally:
            connection.close()

    def robot_call(self, settings, name, arguments, timeout=8):
        token = protect(settings['token_protected'], decrypt=True)
        connection = http.client.HTTPConnection(safe_host(settings['host']), settings['port'], timeout=timeout)
        try:
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': name, 'arguments': arguments}}, ensure_ascii=False).encode('utf-8')
            connection.request('POST', '/mcp', body, {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
            response = connection.getresponse()
            raw = response.read(28001)
            if response.status != 200 or len(raw) > 28000:
                raise RuntimeError('机器人互动响应异常或超过大小限制')
            data = json.loads(raw)
            if data.get('error') or data.get('result', {}).get('isError'):
                raise RuntimeError('机器人拒绝本次互动操作')
            return data
        finally:
            connection.close()

    def start(self):
        with self.lock:
            if self.closed or self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, name='stackchan-data', daemon=True)
            self.thread.start()

    def _sample(self, key, reader):
        try:
            result = reader()
        except Exception as exc:
            with self.lock:
                result = {**self.data.get(key, {}), 'error': str(exc)}
                if not result.get('sampled_at'):
                    result['available'] = False
        with self.lock:
            self.data[key] = result

    def _ai_accounts(self):
        if not hasattr(self.app, 'call'):
            return {'accounts': []}
        result = self.app.call('ai_quota.list')
        if result.get('accounts') and time.monotonic() - self.last_ai_refresh >= 300:
            # Reuse the feature's single-flight job, credentials and rate limit.
            self.app.call('ai_quota.refresh', {})
            self.last_ai_refresh = time.monotonic()
        # Never retain credentials, console cookies or API error bodies here.
        return {'accounts': [{k: a[k] for k in ('name', 'provider', 'status', 'snapshot') if k in a}
                             for a in result.get('accounts', [])]}

    def _page_groups(self, page, paused, config, now):
        # Advance on visits/dwell, not wall-clock modulo. Otherwise an even
        # page-rotation cycle can show the same account group forever.
        key = 'sessions' if page == 'tasks' else 'ai_quota'
        if page in self.group_cursors:
            enter = page != self.group_page
            if enter or (config['rotation_enabled'] and not paused and now - self.group_since >= SPEEDS[config['rotation_speed']]):
                count = max(1, math.ceil(len(recent_tasks(self.data.get('sessions', {}), time.time())) / 4)) if page == 'tasks' else max(1, math.ceil(len(self.data.get(key, {}).get('accounts', [])) / 3))
                if page == 'quota' and self.data.get('quota', {}).get('windows'):
                    count = max(1, math.ceil((len(self.data.get('ai_quota', {}).get('accounts', [])) + 1) / 3))
                self.group_cursors[page] = (self.group_cursors[page] + 1) % count
                self.group_since = now
        self.group_page = page
        return max(0, self.group_cursors['tasks']), max(0, self.group_cursors['quota'])

    def _slow_sample(self, key, reader, revision):
        # Weather/desktop IPC can time out independently; keep the 2s telemetry heartbeat alive.
        with self.lock:
            if key in self.slow_busy:
                return False
            self.slow_busy.add(key)
        def run():
            try:
                failed = False
                try:
                    result = reader()
                except Exception as exc:
                    failed = True
                    with self.lock:
                        result = {**self.data.get(key, {}), 'error': str(exc)}
                        if not result.get('sampled_at'):
                            result['available'] = False
                with self.lock:
                    if not self.closed and revision == self.revision:
                        self.data[key] = result
                        if failed:
                            self.retry_at[key] = time.monotonic() + 60
                        else:
                            self.retry_at.pop(key, None)
                if key == 'sessions' and not failed and not self.closed and revision == self.revision:
                    self.attention.observe(result, dict(self.config))
                if key == 'sessions' and not self.closed and revision == self.revision:
                    try:
                        atomic_json(Path(self.app.data_dir) / 'stackchan-sessions.json',
                                    {'observed_at': time.time(), **{k: v for k, v in result.items() if k != 'rows'},
                                     'rows': [{k: r[k] for k in ('id', 'state', 'updated_at')} for r in result.get('rows', [])]})
                    except OSError:
                        LOG.debug('无法写入会话同步诊断', exc_info=True)
            finally:
                with self.lock:
                    self.slow_busy.discard(key)
        threading.Thread(target=run, name=f'stackchan-{key}', daemon=True).start()
        return True

    def _loop(self):
        try:
            while not self.stop.is_set():
                with self.lock:
                    config = dict(self.config)
                    force, self.force = self.force, False
                    query, self.city_query = self.city_query, None
                    page, revision = self.data['page'], self.revision
                    retry_at = dict(self.retry_at)
                if query:
                    try:
                        found = cities(query)
                        with self.lock:
                            self.candidates, self.data['city_search'] = found, 'done'
                    except Exception as exc:
                        with self.lock:
                            self.data['city_search'] = str(exc)
                if config['enabled']:
                    now = time.monotonic()
                    if force or now - self.last_metric >= (2 if page == 'pc' else 10):
                        self._sample('metrics', lambda: self.metrics.sample(config['gpu']))
                        self.last_metric = now
                    if force or now - self.last_weather >= 1800 or now >= retry_at.get('weather', float('inf')):
                        if self._slow_sample('weather', lambda current=config: self.weather_reader(current), revision):
                            self.last_weather = now
                    if force or now - self.last_quota >= 180 or now >= retry_at.get('quota', float('inf')):
                        if self._slow_sample('quota', lambda current=config: self.quota_reader(current['thread_id']), revision):
                            self.last_quota = now
                    if force or now - self.last_sessions >= 10 or now >= retry_at.get('sessions', float('inf')):
                        if self._slow_sample('sessions', lambda current=config: self.session_reader(current['thread_id'])
                                             if self.session_reader else self.agent_sessions.read(current), revision):
                            self.last_sessions = now
                    if force or now - self.last_ai_list >= 10:
                        if self._slow_sample('ai_quota', self._ai_accounts, revision):
                            self.last_ai_list = now
                    try:
                        self.printer.configure(config, lambda code: protect(code, decrypt=True))
                        printer = self.printer.snapshot()
                    except Exception as exc:
                        printer = {'available': False, 'connected': False, 'configured': bool(config['printer_serial']),
                                   'error': f'打印机连接不可用：{type(exc).__name__}'}
                        with self.printer.lock:
                            self.printer.value = printer
                    with self.lock:
                        self.data['printer'] = printer
                    if now - self.last_printer_diagnostic >= 10:
                        try:
                            atomic_json(Path(self.app.data_dir) / 'stackchan-printer.json',
                                        {'observed_at': time.time(), 'printer': printer})
                            self.last_printer_diagnostic = now
                        except OSError:
                            LOG.debug('无法写入打印机状态诊断', exc_info=True)
                    with self.lock:
                        metrics = copy.deepcopy(self.data['metrics'])
                        sessions = copy.deepcopy(self.data['sessions'])
                        hold_group, hold_quota = self._page_groups(page, self.data.get('rotation_paused'), config, now)
                        gpu = metrics.get('gpu', {})
                        gpu.pop('adapters', None)
                        packet = {'v': 1, 'epoch': time.time(), 'tz_offset': int((time.localtime().tm_gmtoff if hasattr(time.localtime(), 'tm_gmtoff') else -time.altzone if time.daylight and time.localtime().tm_isdst else -time.timezone) / 60),
                                  'default_page': config['default_page'], 'metrics': metrics,
                                  'weather': copy.deepcopy(self.data['weather']), 'quota': copy.deepcopy(self.data['quota']),
                                  'ai_quota': copy.deepcopy(self.data['ai_quota'])}
                    try:
                        packet = panel_packet(packet, sessions, printer, config, hold_group=hold_group, hold_quota=hold_quota)
                        answer = self._send_panel(config, packet)
                        if answer is None:
                            self.wake.wait(1)
                            self.wake.clear()
                            continue
                        if answer.get('v') != 1 or answer.get('page') not in PAGES:
                            raise RuntimeError('机器人桌面协议不兼容')
                        with self.lock:
                            if revision == self.revision:
                                self.health.observe(answer, config)
                                self.data.update(connected=True, page=answer['page'], drawer=bool(answer.get('drawer')),
                                                 last_sent=time.time(), error=None, rotation_paused=bool(answer.get('rotation_paused')),
                                                 session_group=packet['sessions']['page'] - 1, quota_group=packet['ai_quota']['page'] - 1,
                                                 displayed_sessions=copy.deepcopy(packet['sessions']))
                                step = answer.get('quota_step', 0)
                                if self.quota_step is not None and step != self.quota_step:
                                    self.group_cursors['quota'] = packet['ai_quota']['page'] % packet['ai_quota']['pages']
                                    self.group_since = now
                                self.quota_step = step
                        self.interactions.tick(config, sessions, answer)
                        if answer.get('refresh') != self.refresh_seq:
                            if self.refresh_seq is not None:
                                self.last_quota = 0
                                self.last_ai_refresh = 0
                            self.refresh_seq = answer.get('refresh')
                    except Exception as exc:
                        self.interactions.configured = None  # Reapply runtime settings after device reboot/reconnect.
                        with self.lock:
                            self.data.update(connected=False, error=str(exc))
                else:
                    self.printer.close()
                    self.printer.key = None
                    with self.lock:
                        self.data['connected'] = False
                self.wake.wait(2 if config['enabled'] else 30)
                self.wake.clear()
        finally:
            self.metrics.close()
            self.printer.close()

    def close(self):
        self.closed = True
        self.stop.set()
        self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)


def register(app):
    service = DeskService(app)
    app.stackchan = service
    app.register('stackchan.status', service.status)
    app.register('stackchan.settings.save', service.save)
    app.register('stackchan.refresh', service.refresh)
    app.register('stackchan.cities.search', service.search)
    app.register('stackchan.printer.import', service.import_printer)
    app.register('stackchan.welcome.test', lambda _=None: service.interactions.manual_welcome())
    if service.config['enabled']:
        service.start()
