"""Read-only account quota adapters. Credentials never enter jobs, snapshots or RPC replies."""
import copy
import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from ..settings import protect


CATALOG = [
    dict(id='deepseek', name='DeepSeek', kind='balance', auth='key', hosts=['api.deepseek.com'],
         console_url='https://platform.deepseek.com/usage', hint='使用 DeepSeek 开放平台 API Key，查询账户余额。'),
    dict(id='opencode_go', name='OpenCode Go', kind='plan', auth='key', hosts=['opencode.ai'],
         console_url='https://opencode.ai/console', hint='使用 Go 套餐 API Key，读取 5 小时、周度、月度限额。'),
    dict(id='kimi_code', name='Kimi Code', kind='plan', auth='key', hosts=['api.kimi.com'],
         console_url='https://www.kimi.com/code/console',
         hint='使用中国站 Kimi Code 控制台 API Key，查询月度共享额度、5 小时限额及旧套餐周限额。'),
    dict(id='qianwen', name='千问 Token Plan', kind='plan', auth='cookie', hosts=[],
         console_url='https://platform.qianwenai.com/home/analytics/token-plan/individual',
         hint='使用千问控制台登录 Cookie；新版 Token Plan 与阿里云 Coding Plan 分开统计。'),
    dict(id='aliyun_coding', name='阿里云 Coding Plan', kind='plan', auth='cookie', hosts=[],
         console_url='https://bailian.console.aliyun.com/cn-beijing/?tab=model#/efm/coding_plan',
         hint='使用百炼控制台登录 Cookie，读取实际 5 小时、周度和月度请求额度。'),
    dict(id='siliconflow', name='SiliconFlow', kind='balance', auth='key', hosts=['api.siliconflow.cn'],
         console_url='https://cloud.siliconflow.cn/account/ak', hint='使用硅基流动 API Key，查询总余额、充值与赠送余额。'),
    dict(id='moonshot', name='Moonshot', kind='balance', auth='key', hosts=['api.moonshot.cn'],
         console_url='https://platform.moonshot.cn/console/account', hint='使用 Moonshot 开放平台 API Key；此项为 API 余额，不是 Kimi 会员额度。'),
    dict(id='openrouter', name='OpenRouter', kind='balance', auth='key', hosts=['openrouter.ai'],
         console_url='https://openrouter.ai/settings/keys', hint='查询当前 API Key 的额度和消耗；无限额 Key 只显示实际消耗。'),
    dict(id='glm', name='智谱 GLM', kind='plan', auth='key', hosts=['open.bigmodel.cn', 'api.z.ai'],
         console_url='https://bigmodel.cn/coding-plan/personal/overview',
         hint='使用智谱 API Key：读取 Coding Plan 的 5 小时与每周额度，标准 API 余额和 Token 包有值时一并显示。国际站直连会自动切换 z.ai。'),
]
BY_ID = {row['id']: row for row in CATALOG}


class QuotaError(Exception):
    def __init__(self, message, status='error'):
        super().__init__(message)
        self.status = status


def number(value):
    if isinstance(value, bool) or value is None:
        raise QuotaError('平台未返回有效额度数字，请打开控制台核实。')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise QuotaError('平台额度格式发生变化，请打开控制台核实。') from None
    if not math.isfinite(result):
        raise QuotaError('平台返回了无效额度。')
    return result


def timestamp(value):
    if value is None or value == '':
        return None
    try:
        if isinstance(value, (int, float)) or str(value).replace('.', '', 1).isdigit():
            value = float(value)
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc).isoformat()
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def meter(label, used=None, total=None, *, percent=None, reset=None, unit='次'):
    result = dict(label=label, unit=unit, resets_at=timestamp(reset))
    if used is not None and total is not None:
        used, total = number(used), number(total)
        if used < 0 or total <= 0:
            raise QuotaError('平台额度计数不完整，请打开控制台核实。')
        result.update(used=used, total=total, remaining=max(0, total-used))
        percent = used / total * 100
    if percent is None:
        raise QuotaError('平台没有返回此周期的用量。')
    percent = number(percent)
    if percent < 0:
        raise QuotaError('平台返回了负用量比例。')
    result.update(used_percent=percent, remaining_percent=max(0, 100-percent))
    return result


def balance(label, value, currency):
    if currency not in ('CNY', 'USD'):
        raise QuotaError('平台返回了无法识别的货币单位。')
    return dict(label=label, value=number(value), currency=currency)


def kimi_meters(payload):
    """Prefer precise ratio pools over the rounded legacy quota counters."""
    meters = {}
    pools = payload.get('usages') or {}
    if not isinstance(pools, dict):
        raise QuotaError('Kimi Code 额度格式发生变化，请打开控制台核实。')
    for key, label in [('limit_5h', '5 小时'), ('limit_7d', '每周'), ('limit_month_total', '每月')]:
        if key not in pools:
            continue
        row = pools[key]
        if not isinstance(row, dict) or row.get('used_ratio') is None:
            raise QuotaError('Kimi Code 未返回完整的用量比例。')
        meters[label] = meter(label, percent=number(row['used_ratio'])*100,
                              reset=row.get('reset_time'), unit='%')

    def legacy(label, detail):
        if label in meters or not isinstance(detail, dict):
            return
        total = number(detail.get('limit'))
        used = detail.get('used')
        if used is None:
            remaining = number(detail.get('remaining'))
            if remaining < 0 or remaining > total:
                raise QuotaError('Kimi Code 额度计数不完整，请打开控制台核实。')
            used = total-remaining
        # These are quota units, not a count of model calls or tokens.
        meters[label] = meter(label, used, total, reset=detail.get('resetTime'), unit='额度单位')

    legacy('每周', payload.get('usage'))
    for row in payload.get('limits') or []:
        window = row.get('window') or {}
        duration = number(window.get('duration'))
        scale = {'TIME_UNIT_SECOND': 1, 'TIME_UNIT_MINUTE': 60, 'TIME_UNIT_HOUR': 3600,
                 'TIME_UNIT_DAY': 86400}.get(window.get('timeUnit'))
        seconds = duration*scale if scale else None
        label = {18000: '5 小时', 604800: '每周'}.get(seconds)
        if label:
            legacy(label, row.get('detail'))
    return [meters[label] for label in ('5 小时', '每周', '每月') if label in meters]


def unwrap(payload):
    """Unwrap console gateways, rejecting HTTP-200 authentication/business failures."""
    for _ in range(8):
        if not isinstance(payload, dict):
            break
        code = str(payload.get('code', payload.get('errorCode', '')))
        if any(s in code.lower() for s in ('login', 'unauth', 'forbidden', 'tokenexpired')):
            raise QuotaError('控制台会话已过期，请更新登录 Cookie。', 'auth_required')
        if payload.get('success') is False or payload.get('successResponse') is False or code not in ('', '200', '0', 'SUCCESS'):
            raise QuotaError('控制台拒绝了额度查询，请检查会话或前往控制台。')
        if isinstance(payload.get('DataV2'), (dict, str)):
            payload = payload['DataV2']
        elif isinstance(payload.get('data'), (dict, str)):
            payload = payload['data']
        else:
            return payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                break
    if not isinstance(payload, dict):
        raise QuotaError('平台响应格式发生变化，请打开控制台核实。')
    return payload


def parse(provider, payload):
    result = dict(balances=[], meters=[], note=None, plan=None)
    if provider == 'deepseek':
        rows = payload.get('balance_infos')
        if not isinstance(rows, list) or not rows:
            raise QuotaError('DeepSeek 未返回余额信息。')
        for row in rows:
            result['balances'].append(balance('可用余额', row.get('total_balance'), row.get('currency')))
            for field, label in [('topped_up_balance', '充值余额'), ('granted_balance', '赠送余额')]:
                if field in row:
                    result['balances'].append(balance(label, row[field], row.get('currency')))
        result['available'] = payload.get('is_available')
    elif provider == 'opencode_go':
        usage = payload.get('usage', {})
        for key, label in [('rolling', '5 小时'), ('weekly', '每周'), ('monthly', '每月')]:
            row = usage.get(key)
            if isinstance(row, dict) and row.get('percent') is not None:
                result['meters'].append(meter(label, percent=row['percent'], reset=row.get('resetsAt'), unit='%'))
        result['note'] = '套餐额度；不包含 Zen 钱包余额。'
    elif provider == 'kimi_code':
        result['meters'] = kimi_meters(payload)
        result['note'] = '月度额度与其他 Kimi 会员功能共享；仅显示账户实际返回的周期，不换算请求数或 Token 数。'
    elif provider == 'qianwen':
        data = unwrap(payload)
        for prefix, label in [('per5Hour', '5 小时'), ('perWeek', '每周'), ('per1Month', '每月')]:
            if data.get(prefix+'Percentage') is not None:
                result['meters'].append(meter(label, percent=number(data[prefix+'Percentage'])*100,
                                              reset=data.get(prefix+'ResetTime'), unit='%'))
        result['note'] = '按控制台实际扣费比例统计，不换算为请求数或 Token 数。'
    elif provider == 'aliyun_coding':
        data = unwrap(payload)
        rows = data.get('codingPlanInstanceInfos') or []
        if isinstance(rows, list) and len(rows) > 1:
            raise QuotaError('此账户返回多个 Coding Plan 实例，请在控制台确认当前套餐。')
        item = rows[0] if rows else data
        quota = item.get('codingPlanQuotaInfo') or data.get('codingPlanQuotaInfo') or {}
        result['plan'] = item.get('planName') or item.get('instanceName') or item.get('packageName')
        for prefix, label in [('per5Hour', '5 小时'), ('perWeek', '每周'), ('perBillMonth', '每月')]:
            if quota.get(prefix+'UsedQuota') is not None and quota.get(prefix+'TotalQuota') is not None:
                result['meters'].append(meter(label, quota[prefix+'UsedQuota'], quota[prefix+'TotalQuota'],
                                              reset=quota.get(prefix+'QuotaNextRefreshTime')))
    elif provider == 'siliconflow':
        data = unwrap(payload)
        result['balances'].append(balance('可用余额', data.get('totalBalance'), 'CNY'))
        for key, label in [('chargeBalance', '充值余额'), ('balance', '赠送余额')]:
            if key in data:
                result['balances'].append(balance(label, data[key], 'CNY'))
    elif provider == 'moonshot':
        data = unwrap(payload)
        result['balances'].append(balance('可用余额', data.get('available_balance'), 'CNY'))
        for key, label in [('cash_balance', '现金余额'), ('voucher_balance', '代金券')]:
            if key in data:
                result['balances'].append(balance(label, data[key], 'CNY'))
    elif provider == 'openrouter':
        data = unwrap(payload)
        used = number(data.get('usage'))
        limit = data.get('limit')
        if limit is not None:
            limit = number(limit)
            if limit > 0:
                remaining = data.get('limit_remaining')
                period_used = max(0, limit-number(remaining)) if remaining is not None else used
                result['meters'].append(meter('Key 额度', period_used, limit, unit='USD'))
            # Zero is an exhausted key, not unlimited.
            result['balances'].append(balance('Key 剩余额度', data.get('limit_remaining', max(0, limit-used)), 'USD'))
        result['spend'] = dict(value=used, currency='USD')
        result['note'] = '当前 Key 的额度与累计消耗；不代表账户钱包余额。' if limit is not None else '当前 Key 未设消费上限；仅显示已消耗金额。'
    elif provider == 'glm':
        def glm_limits(key):
            row = payload.get(key)
            container = row.get('data', row) if isinstance(row, dict) else row
            if isinstance(container, list):
                return [x for x in container if isinstance(x, dict)]
            return container.get('limits') if isinstance(container, dict) else []

        def glm_find(limits, kind, unit):
            fallback = None
            for item in limits:
                if item.get('type') != kind and item.get('name') != kind:
                    continue
                if unit is None:  # Unfiltered lookups take the first matching window.
                    return item
                if item.get('unit') == unit:
                    return item
                if fallback is None and item.get('unit') is None:
                    fallback = item
            return fallback

        limits = glm_limits('quota')
        session = glm_find(limits, 'TOKENS_LIMIT', 3) or glm_find(limits, 'CREDIT_LIMIT', 3)
        if session and session.get('percentage') is not None:
            result['meters'].append(meter('5 小时', percent=number(session['percentage']), reset=session.get('nextResetTime'), unit='%'))
        rest = [item for item in limits if item is not session]
        weekly = glm_find(rest, 'TOKENS_LIMIT', 6) or glm_find(rest, 'CREDIT_LIMIT', 6)
        if weekly and weekly.get('percentage') is not None:
            result['meters'].append(meter('每周', percent=number(weekly['percentage']), reset=weekly.get('nextResetTime'), unit='%'))
        search = glm_find(rest, 'TIME_LIMIT', None)
        if search and search.get('currentValue') is not None and search.get('usage') is not None:
            try:
                current, the_total = number(search['currentValue']), number(search['usage'])
            except QuotaError:
                current, the_total = None, None
            # Inconsistent counters must not poison the whole quota parse.
            if current is not None and the_total > 0 and 0 <= current <= the_total:
                result['meters'].append(meter('联网搜索', current, the_total, reset=search.get('nextResetTime'), unit='次'))
        currency = payload.get('currency') or 'CNY'
        subscription = payload.get('subscription')
        if isinstance(subscription, dict) and subscription.get('success') is False:
            subscription = None
        subscription = subscription.get('data') if isinstance(subscription, dict) else subscription
        if isinstance(subscription, list) and subscription and isinstance(subscription[0], dict):
            result['plan'] = str(subscription[0].get('productName') or '') or None
            result['expires_at'] = timestamp(subscription[0].get('nextRenewTime'))
        account = payload.get('account')
        if isinstance(account, dict) and account.get('success') is False:
            account = None
        account = account.get('data') if isinstance(account, dict) and isinstance(account.get('data'), dict) else account
        if account and not isinstance(account, dict):
            account = None
        if isinstance(account, dict):
            available = account.get('availableBalance', account.get('balance'))
            if available is not None:
                result['balances'].append(balance('可用余额', available, currency))
            for field, label in [('rechargeAmount', '充值余额'), ('giveAmount', '赠送余额')]:
                if account.get(field) is not None:
                    result['balances'].append(balance(label, account[field], currency))
        tokens = payload.get('tokens')
        tokens = tokens.get('rows') if isinstance(tokens, dict) else tokens
        if isinstance(tokens, dict) and tokens.get('success') is False:
            tokens = None
        for row in tokens or []:
            if not isinstance(row, dict) or row.get('tokenBalance') is None:
                continue
            if row.get('status') not in (None, 'EFFECTIVE'):
                continue
            name = str(row.get('resourcePackageName') or '').strip() or 'Token 包'
            # Platform-raw magnitude: tokenBalance keeps its original scale on purpose.
            result['balances'].append({**balance(name, row['tokenBalance'], currency), 'unit': 'Token 包'})
        if result['meters'] and not result['balances']:
            result['note'] = '套餐额度按平台百分比显示；仅统计实际返回的周期。'
        elif result['meters']:
            result['note'] = '套餐额度按平台百分比显示；标准 API 余额与 Token 包均为平台原始数值。'
        elif result['balances']:
            result['note'] = '未返回 Coding Plan 额度，仅显示标准 API 余额（平台原始数值）。'
    if not result['balances'] and not result['meters'] and 'spend' not in result:
        raise QuotaError('平台未返回可确认的额度，未将缺失值当作零。')
    return result


class AiQuota:
    def __init__(self, app, transport=None, clock=time.time):
        self.app, self.transport, self.clock = app, transport, clock
        self.lock = threading.RLock()
        self._ensure_table()

    def _ensure_table(self):
        with self.app.storage.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ai_quota_accounts_v1(id TEXT PRIMARY KEY, record TEXT NOT NULL)')

    def _all(self):
        self._ensure_table()
        with self.app.storage.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT record FROM ai_quota_accounts_v1').fetchall()]

    def _get(self, account_id):
        self._ensure_table()
        with self.app.storage.connect() as db:
            row = db.execute('SELECT record FROM ai_quota_accounts_v1 WHERE id=?', (account_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, row):
        self._ensure_table()
        with self.app.storage.connect() as db:
            db.execute('INSERT OR REPLACE INTO ai_quota_accounts_v1(id,record) VALUES(?,?)', (row['id'], json.dumps(row, ensure_ascii=False)))

    def catalogue(self, _=None):
        providers = self.app.settings.get()['providers']
        return {'providers': [dict(row, sources=[dict(id=p['id'], name=p['name']) for p in providers
                                                if p.get('has_key') and self.matches(row['id'], p)]) for row in CATALOG]}

    @staticmethod
    def matches(provider, setting):
        url = urlsplit(setting.get('base_url', ''))
        if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
            return False
        if provider == 'opencode_go':
            return url.hostname == 'opencode.ai' and url.path.startswith('/zen/go')
        if provider == 'kimi_code':
            return url.hostname == 'api.kimi.com' and url.path.rstrip('/') in ('/coding', '/coding/v1')
        return url.hostname in BY_ID[provider]['hosts']

    def listing(self, _=None):
        with self.lock:
            rows = self._all()
        return {'accounts': [self.public(row) for row in sorted(rows, key=lambda r: r['created_at'])]}

    @staticmethod
    def public(row):
        return {k: copy.deepcopy(v) for k, v in row.items() if k not in ('credential', 'revision')}

    def save(self, p):
        provider = p.get('provider')
        if provider not in BY_ID:
            raise ValueError('请选择支持的平台。')
        name = str(p.get('name', '')).strip() or BY_ID[provider]['name']
        if len(name) > 80:
            raise ValueError('账户名称最多 80 个字符。')
        with self.lock:
            old = self._get(p.get('id', '')) if p.get('id') else None
            if p.get('id') and not old:
                raise ValueError('账户不存在，请刷新列表。')
            if old and old['provider'] != provider:
                raise ValueError('修改平台请新建账户。')
            source = str(p.get('source_provider_id', '')).strip()
            credential = str(p.get('credential', '')).strip()
            if source:
                config = next((r for r in self.app.settings.get()['providers'] if r['id'] == source), None)
                if not config or not config.get('has_key') or not self.matches(provider, config):
                    raise ValueError('请选择此平台在模型设置中的有效密钥。')
                encrypted = None
            elif credential:
                if len(credential) > 32768 or any(ch in credential for ch in '\r\n\x00'):
                    raise ValueError('凭据格式无效，请粘贴单行 API Key 或 Cookie。')
                if credential.startswith('Cookie:') or credential.startswith('cookie:'):
                    credential = credential.split(':', 1)[1].strip()
                # Console login tickets may contain literal '*'. Only API keys
                # use the masked-key guard; never rewrite a valid cookie value.
                if not credential or '•' in credential or (BY_ID[provider]['auth'] == 'key' and '*' in credential):
                    raise ValueError('请输入真实凭据，不能使用掩码。')
                if BY_ID[provider]['auth'] == 'cookie' and '=' not in credential:
                    raise ValueError('此平台需要控制台 Cookie，不能使用模型 API Key。')
                encrypted = protect(credential)
            elif old and not old.get('source_provider_id'):
                encrypted = old.get('credential')
            elif provider == 'qianwen' and p.get('api_only') is True:
                encrypted = None
            else:
                raise ValueError('请填写凭据或选择已保存的模型服务。')
            changed = not old or bool(credential) or source != old.get('source_provider_id', '')
            now = self.clock()
            row = dict(id=old['id'] if old else uuid.uuid4().hex, provider=provider, name=name,
                       source_provider_id=source, credential=encrypted, created_at=old['created_at'] if old else now,
                       revision=uuid.uuid4().hex, snapshot=None if changed else old.get('snapshot'),
                       status='not_checked' if changed else old.get('status', 'not_checked'),
                       error=None if changed else old.get('error'), checked_at=None if changed else old.get('checked_at'))
            self._put(row)
            return self.public(row)

    def remove(self, p):
        with self.lock:
            self._ensure_table()
            with self.app.storage.connect() as db:
                db.execute('DELETE FROM ai_quota_accounts_v1 WHERE id=?', (p['id'],))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_call_routes_v1'").fetchone():
                    db.execute('DELETE FROM ai_call_routes_v1 WHERE id=?', (p['id'],))
        return {'ok': True}

    def request(self, client, method, url, **kwargs):
        # Fixed endpoints only; redirects must not forward a credential to a login or foreign host.
        with client.stream(method, url, **kwargs) as response:
            if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                raise QuotaError('凭据无效或会话已过期，请重新连接。', 'auth_required')
            if response.status_code == 429:
                raise QuotaError('平台限制了查询频率，请稍后刷新。', 'rate_limited')
            if response.status_code >= 400:
                raise QuotaError(f'平台查询失败（HTTP {response.status_code}），请稍后重试。')
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise QuotaError('额度响应过大，请打开平台控制台。')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise QuotaError('平台没有返回额度 JSON，请检查登录状态。', 'auth_required') from None
        if not isinstance(payload, dict):
            raise QuotaError('平台额度格式发生变化。')
        return payload

    def fetch(self, row):
        provider = row['provider']
        source = None
        if row.get('source_provider_id'):
            source = next((r for r in self.app.settings.get()['providers'] if r['id'] == row['source_provider_id']), None)
            if not source or not self.matches(provider, source):
                raise QuotaError('模型服务已删除或地址已改变，请重新连接。', 'auth_required')
            key = self.app.settings.secret(row['source_provider_id'])
        else:
            try:
                key = protect(row['credential'], decrypt=True)
            except Exception:
                raise QuotaError('无法解密本机凭据，请重新连接此账户。', 'auth_required') from None
        if not key:
            raise QuotaError('未配置查询凭据，请连接账户。', 'auth_required')
        region = 'zai' if urlsplit((source or {}).get('base_url', '')).hostname == 'api.z.ai' else 'bigmodel'
        with httpx.Client(timeout=httpx.Timeout(12, connect=6), follow_redirects=False, transport=self.transport) as client:
            headers = {'Accept': 'application/json', 'User-Agent': 'WinToolbox/0.6 quota-readonly'}
            if BY_ID[provider]['auth'] == 'key':
                headers['Authorization'] = f'Bearer {key}'
                if provider == 'glm':
                    return self.fetch_glm(client, headers, key, region)
                endpoints = dict(deepseek='https://api.deepseek.com/user/balance', opencode_go='https://opencode.ai/zen/go/v1/usage',
                                 kimi_code='https://api.kimi.com/coding/v1/usages',
                                 siliconflow='https://api.siliconflow.cn/v1/user/info', moonshot='https://api.moonshot.cn/v1/users/me/balance',
                                 openrouter='https://openrouter.ai/api/v1/key')
                payload = self.request(client, 'GET', endpoints[provider], headers=headers)
                return parse(provider, payload)
            headers['Cookie'] = key
            if provider == 'qianwen':
                base = 'https://cs-data.qianwenai.com'
                headers.update(Origin='https://platform.qianwenai.com', Referer=BY_ID[provider]['console_url'])
            else:
                base = 'https://bailian.console.aliyun.com'
                headers.update(Origin=base, Referer=BY_ID[provider]['console_url'])
            # Login/session metadata is served by the home gateway. The quota
            # RPC gateway itself redirects this path to a generic error page.
            info_base = 'https://platform-home.qianwenai.com' if provider == 'qianwen' else base
            info = self.request(client, 'GET', info_base+'/tool/user/info.json', headers=headers)
            token = unwrap(info).get('secToken')
            if not token:
                raise QuotaError('无法获取控制台会话，请更新登录 Cookie。', 'auth_required')
            if provider == 'qianwen':
                def query(suffix):
                    api = 'zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/'+suffix
                    data = {'cornerstoneParam': dict(domain='platform.qianwenai.com', consoleSite='QIANWENAI', console='ONE_CONSOLE',
                                                     xsp_lang='zh-CN', protocol='V2', productCode='p_efm')}
                    if suffix == 'subscription':
                        data['commodityCode'] = 'sfm_tokenplansolo_public_cn'
                    return self.request(client, 'POST', base+'/data/api.json', headers=headers,
                                        params=dict(product='sfm_bailian', action='BroadScopeAspnGateway', api=api),
                                        data=dict(product='sfm_bailian', action='BroadScopeAspnGateway', sec_token=token, region='cn-beijing',
                                                  params=json.dumps(dict(Api=api, Data=data, V='1.0'))))
                result = parse(provider, query('usage'))
                try:
                    subscription = unwrap(query('subscription'))
                    result['plan'] = str(subscription.get('specCode') or '') or None
                    result['expires_at'] = timestamp(subscription.get('endTime'))
                except (QuotaError, httpx.HTTPError):
                    result['note'] += ' 套餐详情暂不可用。'
                return result
            action = 'zeldaEasy.broadscope-bailian.codingPlan.queryCodingPlanInstanceInfoV2'
            parameters = dict(Api=action, V='1.0', Data=dict(
                queryCodingPlanInstanceInfoRequest=dict(commodityCode='sfm_codingplan_public_cn', onlyLatestOne=True),
                cornerstoneParam=dict(feTraceId=str(uuid.uuid4()), feURL=BY_ID[provider]['console_url'], protocol='V2',
                                      console='ONE_CONSOLE', productCode='p_efm', domain='bailian.console.aliyun.com',
                                      consoleSite='BAILIAN_ALIYUN', xsp_lang='zh-CN')))
            payload = self.request(client, 'POST', 'https://bailian-cs.console.aliyun.com/data/api.json', headers=headers,
                                   params=dict(action='BroadScopeAspnGateway', product='sfm_bailian', api=action),
                                   data=dict(sec_token=token, region='cn-beijing', params=json.dumps(parameters)))
            return parse(provider, payload)

    def fetch_glm(self, client, headers, key, region):
        """智谱 Coding Plan 用量与标准 API 余额；中国站与国际站路径一致、鉴权头不同。"""
        domains = {'bigmodel': ('https://open.bigmodel.cn', 'CNY'), 'zai': ('https://api.z.ai', 'USD')}
        order = ['bigmodel', 'zai'] if region != 'zai' else ['zai']
        last = None
        for index, target in enumerate(order):
            base, currency = domains[target]
            payload = {'region': target, 'currency': currency}
            try:
                # The usage monitor expects the raw key on the China host; Bearer elsewhere.
                monitor = dict(headers)
                if target == 'bigmodel':
                    monitor['Authorization'] = key
                quota = self.request(client, 'GET', base+'/api/monitor/usage/quota/limit', headers=monitor)
                code = str(quota.get('code', '200')) if isinstance(quota, dict) else '200'
                if code not in ('200', '0', 'SUCCESS', '0200', 'None'):
                    raise QuotaError('智谱额度接口返回失败（'+code+'）。',
                                     'auth_required' if code in ('401', '403', '1113', '1002') or 'auth' in code.lower() or 'login' in code.lower() else '')
                payload['quota'] = quota
                for name, path in [('subscription', '/api/biz/subscription/list'),
                                   ('account', '/api/biz/account/query-customer-account-report'),
                                   ('tokens', '/api/biz/tokenAccounts/list/my?pageNum=1&pageSize=100')]:
                    try:
                        payload[name] = self.request(client, 'GET', base+path, headers=headers)
                    except (QuotaError, httpx.HTTPError):
                        payload[name] = None
                return parse('glm', payload)
            except QuotaError as exc:
                last = exc
                if exc.status != 'auth_required' or index + 1 == len(order):
                    raise
        raise QuotaError(str(last) or '智谱额度查询失败。', 'auth_required')

    def refresh_job(self, job):
        ids = job.params.get('ids')
        with self.lock:
            rows = self._all()
        if ids is not None:
            if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
                raise ValueError('账户列表格式无效。')
            rows = [r for r in rows if r['id'] in ids]
        updated, failed = 0, 0
        for index, row in enumerate(rows):
            job.check_cancelled()
            if row.get('checked_at') is not None and self.clock()-row['checked_at'] < 60:
                continue
            job.progress(index/max(1, len(rows))*100, '正在查询 '+row['name'])
            try:
                snapshot = self.fetch(row)
                row.update(snapshot=dict(snapshot, fetched_at=self.clock()), status='ok', error=None)
            except QuotaError as exc:
                row.update(status=exc.status, error=str(exc))
                failed += 1
            except httpx.HTTPError:
                row.update(status='error', error='无法连接额度接口，请检查网络后重试。')
                failed += 1
            except Exception:
                row.update(status='error', error='额度响应无法解析，请打开控制台核实。')
                failed += 1
            row['checked_at'] = self.clock()
            job.check_cancelled()
            with self.lock:
                current = self._get(row['id'])
                if current and current['revision'] == row['revision']:
                    self._put(row)
                    updated += 1
        job.progress(100, f'已检查 {updated} 个账户' + (f'，{failed} 个未能查询' if failed else ''))
        return dict(updated=updated, failed=failed)

    def submit_refresh(self, p):
        ids = p.get('ids')
        if ids is not None and (not isinstance(ids, list) or any(not isinstance(x, str) for x in ids)):
            raise ValueError('账户列表格式无效。')
        # Share in-flight work across windows; credentials never enter a job record.
        with self.app.jobs.lock:
            for job in self.app.jobs.active.values():
                if job.record['tool'] == 'ai_quota.refresh':
                    active_ids = job.params.get('ids')
                    if active_ids is None or ids is not None and set(ids).issubset(active_ids):
                        return copy.deepcopy(job.record)
            return self.app.jobs.submit('ai_quota.refresh', {'ids': ids} if ids is not None else {})


def register(app):
    feature = AiQuota(app)
    from . import ai_calls
    ai_calls.register(app, feature)
    app.register('ai_quota.catalogue', feature.catalogue)
    app.register('ai_quota.list', feature.listing)
    app.register('ai_quota.save', feature.save)
    app.register('ai_quota.remove', feature.remove)
    app.register('ai_quota.refresh', feature.submit_refresh)
    app.jobs.register('ai_quota.refresh', feature.refresh_job)
