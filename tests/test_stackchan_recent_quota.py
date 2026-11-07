import json
from types import SimpleNamespace

from toolbox.features.stackchan import DEFAULTS, DeskService
from toolbox.features.stackchan_panel import recent_tasks, panel_packet, timestamp
from toolbox.features.stackchan_quota import accounts_for_robot


def test_task_filter_uses_verified_end_and_never_idle_updated_time():
    now = 1793170000
    rows = [dict(id=str(i), title='task', state=state, updated_at=now, **extra) for i, (state, extra) in enumerate([
        ('running', {}), ('waiting', {}), ('idle', {}), ('idle', {'completed_at': now - 3599}),
        ('idle', {'completed_at': now - 3601}), ('done', {'completed_at': (now - 10) * 1000}),
        ('error', {}), ('running', {'stale': True})])]
    value = recent_tasks({'rows': rows}, now)
    assert {r['id'] for r in value} == {'0', '1', '3', '5', '6'}
    assert [r['state'] for r in value] == ['waiting', 'error', 'running', 'done', 'done']
    assert timestamp(now * 1000) == now


def test_accounts_keep_money_separate_and_whitelist_credentials():
    listing = {'accounts': [{'name': '余额账户', 'status': 'auth_required', 'credential': 'never-send',
                            'snapshot': {'fetched_at': 10, 'balances': [{'value': 57.83, 'currency': 'CNY'}]}},
                           {'name': '套餐', 'status': 'ok', 'snapshot': {'fetched_at': 20,
                            'meters': [{'label': '每周', 'remaining_percent': 61.5}]}}]}
    result = accounts_for_robot(listing, {'windows': [{'minutes': 300, 'remaining': 80}], 'sampled_at': 20})
    assert result[0]['meters'] == [{'label': '5h', 'remaining': 80}]
    assert result[1]['balance'] == 57.83 and result[1]['meters'] == []
    assert result[1]['status'] == 'auth_required'
    assert result[2]['meters'][0] == {'label': '7d', 'remaining': 61.5}
    assert 'never-send' not in json.dumps(result)


def test_multi_account_packets_fit_firmware_budget_and_all_groups_accessible():
    now = 1793170000
    rows = [dict(id=str(i), title=''.join(chr(0x4e00 + i * 40 + j) for j in range(40)),
                 state='running', updated_at=now) for i in range(8)]
    accounts = [dict(name='额度账户' + str(i), status='ok', snapshot={'fetched_at': now,
                    'meters': [{'label': '每周', 'remaining_percent': i}]}) for i in range(7)]
    source = dict(v=1, epoch=now, tz_offset=480, ai_quota={'accounts': accounts})
    names = []
    for group in range(3):
        p = panel_packet(source, {'rows': rows}, {}, DEFAULTS, hold_quota=group)
        assert len(json.dumps(p, ensure_ascii=False, separators=(',', ':')).encode()) <= 7000
        assert len(p['glyphs']) <= 100
        names += [a['name'] for a in p['ai_quota']['accounts']]
    assert len(set(names)) == 7


def test_reentering_quota_advances_groups_even_with_even_rotation_cycle(tmp_path):
    service = DeskService(SimpleNamespace(data_dir=tmp_path))
    try:
        service.data['ai_quota'] = {'accounts': [{}] * 5}
        assert service._page_groups('quota', False, DEFAULTS, 100)[1] == 0
        service._page_groups('pc', False, DEFAULTS, 112)
        assert service._page_groups('quota', False, DEFAULTS, 124)[1] == 1
        service._page_groups('pc', False, DEFAULTS, 136)
        assert service._page_groups('quota', False, DEFAULTS, 148)[1] == 0
    finally:
        service.close()


def test_codex_completion_uses_real_turn_time_including_unloaded_chats(monkeypatch):
    from toolbox.features import feishu_bridge, stackchan_panel
    now = 1793170000
    monkeypatch.setattr(stackchan_panel.time, 'time', lambda: now)
    calls = []
    class Client:
        def tool(self, name, params):
            calls.append(name)
            if name == 'list_threads':
                return {'structuredContent': {'threads': [dict(id='unloaded-recent-end', kind='codex', hostId='local',
                        status='notLoaded', updatedAt=now*1000, title='recent')]}}
            return {'structuredContent': {'turns': [{'status': 'completed', 'completedAt': (now-30)*1000}]}}
        def close(self):
            pass
    monkeypatch.setattr(feishu_bridge, 'connect_codex', lambda *_, **__: Client())
    snapshot = stackchan_panel.read_sessions('anchor')
    assert recent_tasks(snapshot, now)[0]['updated_at'] == now-30
    stackchan_panel.read_sessions('anchor')
    assert calls.count('read_thread') == 1, 'unchanged completed chats need no repeated detail reads'


def test_dsh_recent_rename_cannot_make_old_completion_recent(tmp_path, monkeypatch):
    from toolbox.features import stackchan_dsh
    from test_stackchan_dsh import item, page, end
    now = 1793170000
    monkeypatch.setattr(stackchan_dsh.time, 'time', lambda: now)
    current = item()
    current['updatedAt'] = now*1000
    event = {**end(), 'time': (now-7200)*1000}
    client = stackchan_dsh.DshSessions(home=tmp_path)
    client.rpc = lambda method, _: {'items': [current]} if method == 'session/list' else page(event)
    snapshot = client.read()
    assert snapshot['rows'][0]['completed_at'] == now-7200
    assert 'completion' not in snapshot['rows'][0], 'startup must not send old green notices'
    assert recent_tasks(snapshot, now) == []


def test_quota_auto_refresh_reuses_feature_and_five_minute_interval(tmp_path, monkeypatch):
    from toolbox.features import stackchan
    clock, calls = [1000], []
    monkeypatch.setattr(stackchan.time, 'monotonic', lambda: clock[0])
    def call(method, *_):
        calls.append(method)
        return {'accounts': [{'name': 'AI', 'credential': 'secret', 'snapshot': {}}]} if method.endswith('list') else {'id': 'job'}
    service = DeskService(SimpleNamespace(data_dir=tmp_path, call=call))
    try:
        assert 'secret' not in json.dumps(service._ai_accounts())
        clock[0] += 10
        service._ai_accounts()
        assert calls.count('ai_quota.refresh') == 1
        clock[0] += 300
        service._ai_accounts()
        assert calls.count('ai_quota.refresh') == 2
    finally:
        service.close()
