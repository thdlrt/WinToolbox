import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from toolbox.features import ai_quota as quota
from toolbox.settings import Settings
from toolbox.storage import Storage


@pytest.fixture
def feature(tmp_path, monkeypatch):
    # Fixtures only: never read real providers, browser cookies or Codex configuration.
    monkeypatch.setattr(quota, 'protect', lambda value, decrypt=False: value[7:] if decrypt else 'sealed:'+value)
    app = SimpleNamespace(storage=Storage(tmp_path), settings=Settings(tmp_path))
    return quota.AiQuota(app, clock=lambda: 1000)


class Job:
    def __init__(self, **params):
        self.params = params

    def check_cancelled(self):
        pass

    def progress(self, *args):
        pass


def gateway(data):
    return {'code': '200', 'successResponse': True, 'data': {'success': True, 'DataV2': {'data': {'code': 'SUCCESS', 'success': True, 'data': data}}}}


def test_deepseek_multi_currency_and_zero():
    data = quota.parse('deepseek', {'is_available': False, 'balance_infos': [dict(currency='CNY', total_balance='0', granted_balance='0', topped_up_balance='0'), dict(currency='USD', total_balance='2.53')]})
    assert data['balances'][0]['value'] == 0 and data['available'] is False
    assert data['balances'][-1]['currency'] == 'USD'


def zhipu_payload(**extra):
    payload = {
        'region': 'bigmodel', 'currency': 'CNY',
        'quota': {'code': 200, 'data': {'limits': [
            {'type': 'TOKENS_LIMIT', 'unit': 3, 'percentage': 22.5, 'nextResetTime': 1793896762000},
            {'type': 'TOKENS_LIMIT', 'unit': 6, 'percentage': 5, 'nextResetTime': 1794415200000},
            {'type': 'CREDIT_LIMIT', 'unit': 3, 'percentage': 40},
            {'type': 'TIME_LIMIT', 'unit': 8, 'usage': 20, 'currentValue': 8, 'nextResetTime': 1793884800000},
        ]}},
        'subscription': {'code': 200, 'data': [{'productName': 'GLM Coding Pro', 'nextRenewTime': '2026-12-08 12:00:00'}]},
        'account': {'code': 200, 'success': True, 'data': {'balance': 25.3, 'giveAmount': 5, 'availableBalance': 25.3, 'rechargeAmount': 20}},
        'tokens': {'code': 200, 'rows': [{'tokenBalance': 2, 'tokensMagnitude': 1000000, 'status': 'EFFECTIVE',
                                          'resourcePackageName': '50M Token Pack'},
                                         {'tokenBalance': 0, 'status': 'EXPIRED'}]},
    }
    payload.update(extra)
    return payload


def test_glm_meters_plan_and_balances():
    data = quota.parse('glm', zhipu_payload())
    assert [m['label'] for m in data['meters']] == ['5 小时', '每周', '联网搜索']
    assert data['meters'][0]['used_percent'] == pytest.approx(22.5)
    assert data['meters'][0]['resets_at'].endswith('+00:00')
    assert data['meters'][2]['unit'] == '次' and data['meters'][2]['used'] == 8 and data['meters'][2]['total'] == 20
    assert data['plan'] == 'GLM Coding Pro' and data['expires_at'].startswith('2026-12-08')
    assert data['balances'][0]['label'] == '可用余额' and data['balances'][0]['value'] == 25.3
    assert [row['label'] for row in data['balances'][1:]] == ['充值余额', '赠送余额', '50M Token Pack']
    assert data['balances'][-1]['unit'] == 'Token 包'
    assert '原始数值' in data['note']


def test_glm_credits_fallback_tool_limit_bounds_and_failure_flags():
    data = quota.parse('glm', zhipu_payload(
        quota={'code': 200, 'data': {'limits': [{'name': 'CREDIT_LIMIT', 'percentage': 60},
                                               {'type': 'TIME_LIMIT', 'usage': 0, 'currentValue': 9}]}}))
    assert [m['label'] for m in data['meters']] == ['5 小时']
    assert data['meters'][0]['remaining_percent'] == pytest.approx(40)


def test_glm_without_any_quota_numbers_means_error():
    with pytest.raises(quota.QuotaError):
        quota.parse('glm', {'region': 'bigmodel', 'currency': 'CNY', 'quota': {'code': 200, 'data': {'limits': []}}})


def test_glm_failed_supplementary_requests_never_poison_quota(feature):
    row = feature.save(dict(provider='glm', credential='zhipu-private-key'))
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        expected = 'zhipu-private-key' if request.url.path.endswith('/quota/limit') else 'Bearer zhipu-private-key'
        assert request.headers['authorization'] == expected  # China monitor wants the raw key; others use Bearer
        if request.url.host == 'open.bigmodel.cn':
            payload = {'code': 200, 'data': {'limits': [{'type': 'TOKENS_LIMIT', 'unit': 6, 'percentage': 55}]}} \
                if request.url.path.endswith('/quota/limit') else {'code': 200, 'success': False}
        else:
            raise AssertionError('China key with explicit region must not retry international host')
        return httpx.Response(200, json=payload)
    feature.transport = httpx.MockTransport(handler)
    result = feature.fetch(feature._get(row['id']))
    assert set(hosts) == {'open.bigmodel.cn'}
    assert result['meters'][0]['label'] == '每周'
    assert '仅统计实际返回的周期' in result['note']


def test_glm_pasted_key_falls_back_to_international_station(feature):
    row = feature.save(dict(provider='glm', credential='zhipu-private-key'))
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == 'open.bigmodel.cn':
            assert request.headers['authorization'] == 'zhipu-private-key'
            return httpx.Response(401)
        assert request.headers['authorization'] == 'Bearer zhipu-private-key'
        return httpx.Response(200, json={'code': 200, 'data': {'limits': [{'type': 'TOKENS_LIMIT', 'unit': 3, 'percentage': 12}]}})
    feature.transport = httpx.MockTransport(handler)
    result = feature.fetch(feature._get(row['id']))
    assert hosts[:2] == ['open.bigmodel.cn', 'api.z.ai'] and set(hosts[2:]) == {'api.z.ai'}
    assert result['meters'][0]['remaining_percent'] == pytest.approx(88)


def test_glm_source_provider_region_skips_fallback(feature, monkeypatch):
    providers = [
        {'id': 'zhipu', 'name': '智谱', 'has_key': True, 'base_url': 'https://open.bigmodel.cn/api/paas/v4'},
        {'id': 'zai', 'name': 'Z.ai', 'has_key': True, 'base_url': 'https://api.z.ai/api/paas/v4'},
        {'id': 'mirror', 'name': 'Mirror', 'has_key': True, 'base_url': 'https://glm.example.com/api/paas/v4'},
    ]
    monkeypatch.setattr(feature.app.settings, 'get', lambda: {'providers': providers})
    platform = next(p for p in feature.catalogue()['providers'] if p['id'] == 'glm')
    assert [s['id'] for s in platform['sources']] == ['zhipu', 'zai']
    for url in ['https://open.bigmodel.cn.evil.example/api/paas/v4', 'https://bigmodel.cn/api/paas/v4',
                'http://open.bigmodel.cn/api/paas/v4']:
        assert not feature.matches('glm', {'base_url': url})



def test_go_windows_and_reset():
    data = quota.parse('opencode_go', {'usage': {name: {'percent': v, 'resetsAt': '2026-10-24T00:00:00Z'} for name, v in [('rolling', 0), ('weekly', 81), ('monthly', 100)]}})
    assert [m['remaining_percent'] for m in data['meters']] == [100, 19, 0]
    assert data['meters'][0]['resets_at'].endswith('+00:00')


def test_kimi_current_ratios_take_precedence_over_rounded_counters():
    data = quota.parse('kimi_code', {
        'limits': [{'window': {'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'},
                    'detail': {'limit': '100', 'used': '3', 'remaining': '97'}}],
        'usages': {
            'limit_5h': {'used_ratio': 0.0282, 'reset_time': '2026-10-25T07:59:25Z'},
            'limit_month_total': {'used_ratio': 0.0023, 'reset_time': '2026-11-25T00:00:00Z'},
            'limit_month_code': {'used_ratio': 0.99},
        },
    })
    assert [m['label'] for m in data['meters']] == ['5 小时', '每月']
    assert [m['remaining_percent'] for m in data['meters']] == pytest.approx([97.18, 99.77])
    assert all('used' not in m for m in data['meters'])
    assert data['meters'][1]['resets_at'] == '2026-11-25T00:00:00+00:00'
    assert '共享' in data['note']


def test_kimi_legacy_weekly_and_window_identification():
    data = quota.parse('kimi_code', {
        'usage': {'limit': '2048', 'remaining': '1834', 'resetTime': 1793491200000},
        'limits': [
            {'window': {'duration': 1, 'timeUnit': 'TIME_UNIT_MINUTE'}, 'detail': {'limit': '9', 'used': '8'}},
            {'window': {'duration': 5, 'timeUnit': 'TIME_UNIT_HOUR'}, 'detail': {'limit': '200', 'used': '139'}},
        ],
    })
    assert [m['label'] for m in data['meters']] == ['5 小时', '每周']
    assert data['meters'][0]['remaining_percent'] == pytest.approx(30.5)
    assert data['meters'][1]['remaining'] == 1834
    assert data['meters'][1]['unit'] == '额度单位'


@pytest.mark.parametrize('ratio,remaining', [(0, 100), (1, 0), (1.2, 0)])
def test_kimi_zero_exhausted_and_overage_ratios(ratio, remaining):
    data = quota.parse('kimi_code', {'usages': {'limit_month_total': {'used_ratio': ratio}}})
    assert data['meters'][0]['remaining_percent'] == remaining


@pytest.mark.parametrize('payload', [
    {}, {'usages': {}}, {'usages': {'limit_month_code': {'used_ratio': 0}}},
    {'usages': {'limit_5h': {}}}, {'usages': {'limit_5h': {'used_ratio': True}}},
    {'usages': {'limit_month_total': {'used_ratio': -0.1}}},
    {'usage': {'limit': '0', 'remaining': '0'}},
    {'usage': {'limit': '100', 'remaining': '101'}},
])
def test_kimi_missing_or_invalid_data_never_means_full_quota(payload):
    with pytest.raises(quota.QuotaError):
        quota.parse('kimi_code', payload)


def test_kimi_key_is_bound_to_domestic_code_service(feature, monkeypatch):
    providers = [
        {'id': 'kimi', 'name': 'Kimi Code', 'has_key': True, 'base_url': 'https://api.kimi.com/coding/v1/'},
        {'id': 'moonshot', 'name': 'Moonshot', 'has_key': True, 'base_url': 'https://api.moonshot.cn/v1'},
        {'id': 'kimi-web', 'name': 'Other', 'has_key': True, 'base_url': 'https://api.kimi.com/v1'},
    ]
    monkeypatch.setattr(feature.app.settings, 'get', lambda: {'providers': providers})
    platform = next(p for p in feature.catalogue()['providers'] if p['id'] == 'kimi_code')
    assert platform['sources'] == [{'id': 'kimi', 'name': 'Kimi Code'}]
    for url in ['https://api.kimi.com.evil.example/coding/v1', 'https://api.kimi.ai/coding/v1',
                'http://api.kimi.com/coding/v1', 'https://api.kimi.com/coding/other']:
        assert not feature.matches('kimi_code', {'base_url': url})


def test_kimi_fetch_only_reads_official_quota_endpoint_and_redacts_key(feature):
    row = feature.save(dict(provider='kimi_code', credential='kimi-private-fixture'))
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert str(request.url) == 'https://api.kimi.com/coding/v1/usages'
        assert request.headers['authorization'] == 'Bearer kimi-private-fixture'
        return httpx.Response(200, json={'usages': {'limit_5h': {'used_ratio': 0.2}}})
    feature.transport = httpx.MockTransport(handler)
    assert feature.refresh_job(Job(ids=[row['id']])) == {'updated': 1, 'failed': 0}
    public = feature.listing()
    assert public['accounts'][0]['snapshot']['meters'][0]['remaining_percent'] == 80
    assert len(requests) == 1 and 'kimi-private-fixture' not in json.dumps(public)


def test_qianwen_actual_nested_payload_has_fraction_not_percent():
    data = quota.parse('qianwen', gateway({'per1MonthPercentage': 0.05406790955555556, 'per1MonthResetTime': 1795449600000}))
    assert data['meters'][0]['remaining_percent'] == pytest.approx(94.59320904444444)
    assert 'used' not in data['meters'][0]  # No invented request/token count.
    assert data['meters'][0]['resets_at'].startswith('2026-11-23T16:00')


def test_aliyun_actual_counters():
    data = quota.parse('aliyun_coding', gateway({'codingPlanInstanceInfos': [{'planName': 'Pro', 'codingPlanQuotaInfo': {
        'per5HourUsedQuota': 42, 'per5HourTotalQuota': 6000, 'per5HourQuotaNextRefreshTime': 1795449600000,
        'perWeekUsedQuota': 123, 'perWeekTotalQuota': 45000, 'perBillMonthUsedQuota': 456, 'perBillMonthTotalQuota': 90000}}]}))
    assert data['plan'] == 'Pro' and len(data['meters']) == 3
    assert data['meters'][0]['remaining'] == 5958


@pytest.mark.parametrize('provider,payload', [('deepseek', {}), ('opencode_go', {'usage': {}}), ('qianwen', gateway({})), ('aliyun_coding', gateway({'codingPlanInstanceInfos': [{'planName': 'Pro'}]})), ('siliconflow', {'code': 200, 'data': {}}), ('moonshot', {'data': {}}), ('openrouter', {'data': {}})])
def test_missing_data_never_becomes_full_quota(provider, payload):
    with pytest.raises(quota.QuotaError):
        quota.parse(provider, payload)


@pytest.mark.parametrize('value', [None, True, 'NaN', float('inf'), 'invalid'])
def test_invalid_numeric_values(value):
    with pytest.raises(quota.QuotaError):
        quota.number(value)


def test_openrouter_period_remaining_is_not_lifetime_spend():
    data = quota.parse('openrouter', {'data': {'usage': 350, 'limit': 50, 'limit_remaining': 40}})
    assert data['meters'][0]['remaining_percent'] == 80
    assert data['spend']['value'] == 350
    assert quota.parse('openrouter', {'data': {'usage': 5, 'limit': None}})['meters'] == []
    assert quota.parse('openrouter', {'data': {'usage': 5, 'limit': 0}})['balances'][0]['value'] == 0


def test_save_encrypts_redacts_and_blank_edit_preserves(feature):
    row = feature.save(dict(provider='deepseek', credential='test-secret'))
    assert 'credential' not in row and 'test-secret' not in json.dumps(feature.listing())
    saved = feature._get(row['id'])
    assert saved['credential'] == 'sealed:test-secret'
    feature.save(dict(id=row['id'], provider='deepseek', name='工作', credential=''))
    assert feature._get(row['id'])['credential'] == saved['credential']
    feature.remove({'id': row['id']})
    assert feature.listing()['accounts'] == []


def test_provider_reuse_is_host_bound(feature, monkeypatch):
    providers = [{'id': 'ds', 'name': 'DeepSeek', 'has_key': True, 'base_url': 'https://api.deepseek.com/v1'},
                 {'id': 'proxy', 'name': '代理', 'has_key': True, 'base_url': 'https://api.deepseek.com.evil.example/v1'}]
    monkeypatch.setattr(feature.app.settings, 'get', lambda: {'providers': providers})
    assert feature.catalogue()['providers'][0]['sources'] == [{'id': 'ds', 'name': 'DeepSeek'}]
    feature.save(dict(provider='deepseek', source_provider_id='ds'))
    with pytest.raises(ValueError):
        feature.save(dict(provider='openrouter', source_provider_id='ds'))
    assert not feature.matches('opencode_go', {'base_url': 'https://opencode.ai/zen/v1'})


def test_refresh_retains_old_snapshot_on_auth_failure_and_continues(feature):
    first = feature.save(dict(provider='deepseek', credential='ds-key'))
    second = feature.save(dict(provider='opencode_go', credential='go-key'))
    def handler(request):
        if request.url.host == 'api.deepseek.com':
            return httpx.Response(401, json={'message': 'do not expose ds-key'})
        return httpx.Response(200, json={'usage': {'monthly': {'percent': 20}}})
    feature.transport = httpx.MockTransport(handler)
    row = feature._get(first['id']); row['snapshot'] = {'fetched_at': 100, 'balances': [{'value': 10, 'currency': 'CNY'}], 'meters': []}; feature._put(row)
    result = feature.refresh_job(Job())
    assert result == {'updated': 2, 'failed': 1}
    rows = {r['id']: r for r in feature.listing()['accounts']}
    assert rows[first['id']]['status'] == 'auth_required' and rows[first['id']]['snapshot']['fetched_at'] == 100
    assert rows[second['id']]['snapshot']['meters'][0]['remaining_percent'] == 80
    assert 'ds-key' not in json.dumps(rows)
    # Rate limit applies to repeated UI/job requests, including failures.
    assert feature.refresh_job(Job()) == {'updated': 0, 'failed': 0}


def test_console_expiry_http_200_is_auth_error():
    with pytest.raises(quota.QuotaError) as err:
        quota.parse('qianwen', {'code': 'ConsoleNeedLogin', 'successResponse': False})
    assert err.value.status == 'auth_required'


def test_cookie_ticket_can_contain_literal_asterisks(feature):
    row = feature.save(dict(provider='qianwen', credential='login_qianwenai_ticket=literal*login*ticket; cna=test'))
    assert feature._get(row['id'])['credential'] == 'sealed:login_qianwenai_ticket=literal*login*ticket; cna=test'
    assert 'credential' not in row
    with pytest.raises(ValueError):
        feature.save(dict(provider='deepseek', credential='sk-****'))


def test_cookies_are_scoped_and_form_encoding_is_preserved(feature):
    row = feature.save(dict(provider='qianwen', credential='session=fixture; other=two'))
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers['cookie'] == 'session=fixture; other=two'
        if request.url.path.endswith('info.json'):
            assert request.url.host == 'platform-home.qianwenai.com'
            return httpx.Response(200, json={'code': '200', 'data': {'secToken': 'a+b&c=1'}})
        assert request.url.host == 'cs-data.qianwenai.com'
        form = parse_qs(request.content.decode())
        assert form['sec_token'] == ['a+b&c=1']
        params = json.loads(form['params'][0])
        assert params['Data']['cornerstoneParam']['domain'] == 'platform.qianwenai.com'
        if params['Api'].endswith('/subscription'):
            return httpx.Response(200, json=gateway({'specCode': 'standard', 'endTime': 1795449600000}))
        return httpx.Response(200, json=gateway({'per1MonthPercentage': 0.05, 'per1MonthResetTime': 1795449600000}))
    feature.transport = httpx.MockTransport(handler)
    result = feature.fetch(feature._get(row['id']))
    assert result['plan'] == 'standard' and result['meters'][0]['remaining_percent'] == 95
    assert len(requests) == 3


def test_redirect_does_not_forward_key(feature):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={'location': 'https://foreign.example'})
    row = feature.save(dict(provider='deepseek', credential='test-key'))
    feature.transport = httpx.MockTransport(handler)
    with pytest.raises(quota.QuotaError):
        feature.fetch(feature._get(row['id']))
    assert len(seen) == 1


def test_deleted_or_edited_inflight_account_is_not_resurrected(feature, monkeypatch):
    row = feature.save(dict(provider='deepseek', credential='test-key'))
    def fetch(account):
        feature.remove({'id': account['id']})
        return {'balances': [], 'meters': []}
    monkeypatch.setattr(feature, 'fetch', fetch)
    assert feature.refresh_job(Job(ids=[row['id']]))['updated'] == 0
    assert feature.listing()['accounts'] == []


def test_aliyun_form_uses_console_gateway_and_latest_plan(feature):
    row = feature.save(dict(provider='aliyun_coding', credential='sid=fixture'))
    def handler(request):
        if request.url.path.endswith('info.json'):
            assert request.url.host == 'bailian.console.aliyun.com'
            return httpx.Response(200, json={'code': '200', 'data': {'secToken': 'session-token'}})
        assert request.url.host == 'bailian-cs.console.aliyun.com'
        assert request.url.params['action'] == 'BroadScopeAspnGateway'
        data = json.loads(parse_qs(request.content.decode())['params'][0])
        assert data['Data']['queryCodingPlanInstanceInfoRequest'] == {'commodityCode': 'sfm_codingplan_public_cn', 'onlyLatestOne': True}
        return httpx.Response(200, json=gateway({'codingPlanQuotaInfo': {'per5HourUsedQuota': 1, 'per5HourTotalQuota': 2}}))
    feature.transport = httpx.MockTransport(handler)
    assert feature.fetch(feature._get(row['id']))['meters'][0]['remaining_percent'] == 50


def test_real_rpc_job_lifecycle_and_inflight_deduplication(feature):
    import threading
    import time
    from toolbox.jobs import Jobs
    feature.app.data_dir = feature.app.storage.path.parent
    feature.app.emit = lambda *a, **kw: None
    manager = feature.app.jobs = Jobs(feature.app)
    manager.register('ai_quota.refresh', feature.refresh_job)
    row = feature.save(dict(provider='deepseek', credential='private-fixture'))
    entered, release = threading.Event(), threading.Event()
    def handler(request):
        entered.set()
        assert release.wait(3)
        return httpx.Response(200, json={'balance_infos': [{'currency': 'CNY', 'total_balance': '1.25'}]})
    feature.transport = httpx.MockTransport(handler)
    try:
        job = feature.submit_refresh({'credential': 'must-not-be-stored'})
        assert entered.wait(2)
        same = feature.submit_refresh({'ids': [row['id']]})
        assert same['id'] == job['id']
        assert job['params'] == {} and 'private-fixture' not in json.dumps(manager.list())
        release.set()
        until = time.monotonic() + 3
        while time.monotonic() < until and manager.get(job['id'])['status'] in ('queued', 'running'):
            time.sleep(.01)
        assert manager.get(job['id'])['status'] == 'completed'
        assert feature.listing()['accounts'][0]['snapshot']['balances'][0]['value'] == 1.25
    finally:
        release.set()
        manager.close()
