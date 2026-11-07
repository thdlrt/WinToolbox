"""Feishu listener authorization and configuration tests."""
from unittest.mock import Mock, patch

import pytest

from toolbox.features.feishu_bridge import (FeishuBridge, dsh_command, matching_rule, render_prompt,
                                            validate_config)


def config():
    return validate_config({'enabled': True, 'app_id': 'cli_bot123', 'bot_open_id': 'ou_bot123',
        'rules': [
            {'id': 'owner', 'name': '主人私聊', 'enabled': True, 'chat_type': 'p2p',
             'chat_id': 'oc_private123', 'sender_id': 'ou_owner123', 'require_bot_mention': False,
             'target_thread_id': '01a0eb53-21a8-7522-bf57-19dd0c4daeca',
             'message_types': ['text', 'image'], 'prompt': '处理 {message_id}，发送者 {sender_id}', 'ack_text': ''},
            {'id': 'group', 'name': '项目群', 'enabled': True, 'chat_type': 'group',
             'chat_id': 'oc_group123', 'sender_id': '', 'require_bot_mention': True,
             'target_thread_id': '01a0e848-f0c2-7800-b864-c674bd404610',
             'message_types': ['text', 'post'], 'prompt': '处理群消息 {message_id}', 'ack_text': ''},
        ]})


def event(**changes):
    return {'type': 'im.message.receive_v1', 'chat_type': 'p2p', 'chat_id': 'oc_private123',
            'sender_type': 'user', 'sender_id': 'ou_owner123', 'message_type': 'text',
            'message_id': 'om_test123', 'mentions': [], **changes}


def test_private_rule_requires_exact_sender_and_chat():
    value = config()
    assert matching_rule(event(), value)['id'] == 'owner'
    assert matching_rule(event(message_type='image'), value)['id'] == 'owner'
    for change in ({'sender_id': 'ou_other'}, {'chat_id': 'oc_other'}, {'sender_type': 'bot'},
                   {'chat_type': 'group'}, {'message_type': 'interactive'}, {'message_id': 'bad'}):
        assert matching_rule(event(**change), value) is None
    assert matching_rule(event(chat_type='group', chat_id='oc_group123',
                               mentions=[{'id': 'ou_bot123'}]), value)['id'] == 'group'
    assert matching_rule(event(chat_type='group', chat_id='oc_group123'), value) is None
    value['rules'][0]['keyword'] = '状态'
    assert matching_rule(event(content='看看状态'), value)['id'] == 'owner'
    assert matching_rule(event(content='你好'), value) is None


def test_validation_rejects_unrestricted_private_rule():
    value = config()
    value['rules'][0]['sender_id'] = ''
    with pytest.raises(ValueError, match='发送者'):
        validate_config(value)


def test_prompt_has_verified_message_pointer_and_template_values():
    prompt = render_prompt(config()['rules'][0], event(), 'om_ack123')
    assert 'om_test123' in prompt and 'ou_owner123' in prompt
    assert '重新读取原消息' in prompt
    assert '<message_id>:answer' in prompt


class FakeApp:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.emitted = []

    def emit(self, *args, **kwargs):
        self.emitted.append((args, kwargs))


def test_config_persists_and_job_deduplicates(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    try:
        with patch.object(service, 'start'):
            saved = service.save({'config': config()})
        assert saved['config']['enabled'] is True
        assert service.path.is_file()
        assert service._claim('om_one', 'owner') is True
        assert service._claim('om_one', 'owner') is False
    finally:
        service.close()


def test_dispatched_message_rechecks_sender_before_codex(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    source = event()
    config_value = config()
    reply = {'ok': True, 'data': {'messages': [{
        'message_id': source['message_id'], 'chat_id': source['chat_id'],
        'sender': {'id': 'ou_attacker', 'sender_type': 'user'}, 'content': 'bad'}]}}
    try:
        with patch('toolbox.features.feishu_bridge.lark_json', return_value=reply), \
             patch('toolbox.features.feishu_bridge.connect_codex') as connect, \
             patch.object(service, '_blocked'):
            service._claim(source['message_id'], 'owner')
            service._process(source, config_value['rules'][0], config_value)
            connect.assert_not_called()
            assert service.status()['recent'][0]['status'] == 'failed'
    finally:
        service.close()


OTHER_THREAD = '01a0eb1f-0b44-7f01-9b3f-dec88469a707'


@pytest.fixture
def bridge(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    yield service
    service.close()


def message(mid='om_parent', **changes):
    return {'message_id': mid, 'chat_id': 'oc_private123', 'content': '正文',
            'sender': {'id': 'cli_bot123', 'sender_type': 'app'}, **changes}


def response(*messages):
    return {'ok': True, 'data': {'messages': list(messages)}}


def test_plain_dm_and_group_keep_configured_targets(bridge):
    cfg = config()
    for rule in cfg['rules']:
        assert bridge._resolve_context(message(), rule, cfg) == (rule['target_thread_id'], '')


def test_group_reply_selects_known_source_without_requiring_another_mention(bridge):
    cfg = config()
    rule = cfg['rules'][1]
    source = event(chat_type='group', chat_id=rule['chat_id'], sender_id='ou_member123', reply_to='om_question')
    assert matching_rule(source, cfg, allow_group_reply=True) == rule
    assert matching_rule({**source, 'sender_type': 'app'}, cfg, allow_group_reply=True) is None
    bridge._remember_context('om_question', rule, cfg, OTHER_THREAD, 'test')
    assert bridge._resolve_context(message(reply_to='om_question'), rule, cfg) == (OTHER_THREAD, 'om_question')


def test_group_question_receipt_wins_over_older_discussion_default(bridge):
    cfg = config()
    rule = cfg['rules'][1]
    bridge._remember_context('om_old_discussion', rule, cfg, rule['target_thread_id'], 'dispatch')
    question = message('om_question', chat_id=rule['chat_id'], reply_to='om_old_discussion')
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question)), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD) as find:
        assert bridge._resolve_context(message(reply_to='om_question'), rule, cfg) == (OTHER_THREAD, 'om_question')
        find.assert_called_once_with(question)
    assert bridge._known_context('om_question', rule, cfg) == OTHER_THREAD
    assert bridge._known_context('om_old_discussion', rule, cfg) == rule['target_thread_id']


def test_group_followup_from_another_member_keeps_question_source(bridge):
    cfg = config()
    rule = cfg['rules'][1]
    bridge._remember_context('om_question', rule, cfg, OTHER_THREAD, 'test')
    member_reply = message('om_member_reply', chat_id=rule['chat_id'], reply_to='om_question',
                           sender={'id': 'ou_member123', 'sender_type': 'user'})
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response(member_reply)):
        assert bridge._resolve_context(message(reply_to='om_member_reply'), rule, cfg) == (OTHER_THREAD, 'om_member_reply')
    assert bridge._known_context('om_member_reply', rule, cfg) == OTHER_THREAD


def test_unknown_group_bot_question_never_uses_old_default(bridge):
    cfg = config()
    rule = cfg['rules'][1]
    bridge._remember_context('om_old_discussion', rule, cfg, rule['target_thread_id'], 'dispatch')
    question = message(chat_id=rule['chat_id'], reply_to='om_old_discussion')
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question)), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=None):
        with pytest.raises(RuntimeError, match='不会派发到默认'):
            bridge._resolve_context(message(reply_to='om_parent'), rule, cfg)


def test_legacy_group_default_mapping_is_recovered_from_real_question_receipt(bridge):
    cfg = config()
    rule = cfg['rules'][1]
    bridge.db.execute('INSERT INTO message_contexts VALUES (?,?,?,?,?,?)',
                      (cfg['app_id'], rule['chat_id'], 'om_parent', rule['target_thread_id'], 'dispatch', 1))
    bridge.db.commit()
    assert bridge._known_context('om_parent', rule, cfg) is None
    parent = message(chat_id=rule['chat_id'], reply_to='om_question',
                     sender={'id': 'ou_member123', 'sender_type': 'user'})
    question = message('om_question', chat_id=rule['chat_id'])
    with patch('toolbox.features.feishu_bridge.lark_json', side_effect=[response(parent), response(question)]), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD):
        assert bridge._resolve_context(message(reply_to='om_parent'), rule, cfg)[0] == OTHER_THREAD
    assert bridge._known_context('om_parent', rule, cfg) == OTHER_THREAD
    with pytest.raises(RuntimeError, match='冲突'):
        bridge._remember_context('om_parent', rule, cfg, rule['target_thread_id'], 'dispatch')


@pytest.mark.parametrize('parent_sender', [
    {'id': 'ou_somebody', 'sender_type': 'user'},
    {'id': 'cli_otherbot', 'sender_type': 'app'},
])
def test_unrelated_group_reply_is_silent_and_sends_no_ack(bridge, parent_sender):
    cfg = config()
    rule = cfg['rules'][1]
    source = event(chat_type='group', chat_id=rule['chat_id'], sender_id='ou_member123',
                   reply_to='om_parent', _implicit_group_reply=True)
    incoming = message(source['message_id'], chat_id=rule['chat_id'], reply_to='om_parent',
                       sender={'id': source['sender_id'], 'sender_type': 'user'})
    parent = message(chat_id=rule['chat_id'], sender=parent_sender)
    bridge._claim(source['message_id'], rule['id'])
    with patch('toolbox.features.feishu_bridge.lark_json', side_effect=[response(incoming), response(parent)]) as lark, \
         patch('toolbox.features.feishu_bridge.connect_codex') as connect, \
         patch.object(bridge, '_blocked') as blocked:
        bridge._process(source, rule, cfg)
        assert lark.call_count == 2
        connect.assert_not_called()
        blocked.assert_not_called()
        assert bridge.status()['recent'][0]['status'] == 'ignored'


def test_group_dispatch_rechecks_reply_and_maps_request_ack_answer(bridge):
    cfg = config()
    rule = cfg['rules'][1]
    source = event(chat_type='group', chat_id=rule['chat_id'], sender_id='ou_member123', reply_to='om_question')
    incoming = message(source['message_id'], chat_id=rule['chat_id'], reply_to='om_question',
                       sender={'id': source['sender_id'], 'sender_type': 'user'})
    bridge._remember_context('om_question', rule, cfg, OTHER_THREAD, 'test')
    bridge._claim(source['message_id'], rule['id'])
    with patch('toolbox.features.feishu_bridge.lark_json', side_effect=[response(incoming),
            {'ok': True, 'data': {'message_id': 'om_ack'}},
            response(message('om_answer', chat_id=rule['chat_id'], reply_to=source['message_id']))]), \
         patch('toolbox.features.feishu_bridge.connect_codex') as connect:
        bridge._process(source, rule, cfg)
        connect.assert_called_once_with(OTHER_THREAD, launch=True)
        prompt = connect.return_value.tool.call_args.args[1]['prompt']
        assert '本条群聊是回复消息' in prompt and '不代表主人私聊授权' in prompt
        for mid in (source['message_id'], 'om_ack', 'om_answer'):
            assert bridge._known_context(mid, rule, cfg) == OTHER_THREAD
        assert bridge.status()['recent'][0]['status'] == 'sent'


def test_group_missing_verified_reply_relation_cannot_use_default(bridge):
    cfg = config()
    rule = cfg['rules'][1]
    source = event(chat_type='group', chat_id=rule['chat_id'], sender_id='ou_member123', reply_to='om_question')
    incoming = message(source['message_id'], chat_id=rule['chat_id'],
                       sender={'id': source['sender_id'], 'sender_type': 'user'})
    bridge._claim(source['message_id'], rule['id'])
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response(incoming)), \
         patch('toolbox.features.feishu_bridge.connect_codex') as connect, patch.object(bridge, '_blocked'):
        bridge._process(source, rule, cfg)
        connect.assert_not_called()
        assert '复核回复关系' in bridge.status()['recent'][0]['detail']


@pytest.mark.parametrize('rule_index', [0, 1])
def test_known_reply_context_survives_restart_and_default_change(tmp_path, rule_index):
    cfg = config()
    rule = cfg['rules'][rule_index]
    service = FeishuBridge(FakeApp(tmp_path))
    service._remember_context('om_parent', rule, cfg, OTHER_THREAD, 'test')
    service.close()
    service = FeishuBridge(FakeApp(tmp_path))
    try:
        assert service._resolve_context(message(reply_to='om_parent'), rule, cfg) == (OTHER_THREAD, 'om_parent')
        assert service._resolve_context(message(), rule, cfg)[0] == rule['target_thread_id']
        with pytest.raises(RuntimeError, match='冲突'):
            service._remember_context('om_parent', rule, cfg, rule['target_thread_id'], 'test')
    finally:
        service.close()


def test_reply_chain_prefers_immediate_parent_and_keeps_delegated_answer_in_original_context(bridge):
    cfg = config()
    rule = cfg['rules'][0]
    bridge._remember_context('om_request', rule, cfg, OTHER_THREAD, 'dispatch')
    bridge._remember_context('om_root', rule, cfg, rule['target_thread_id'], 'dispatch')
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response(message(reply_to='om_request'))), \
         patch('toolbox.features.feishu_bridge.find_source_thread') as find:
        assert bridge._resolve_context(message(reply_to='om_parent', root_id='om_root'), rule, cfg)[0] == OTHER_THREAD
        find.assert_not_called()
        assert bridge._known_context('om_parent', rule, cfg) == OTHER_THREAD


def test_standalone_and_legacy_notification_provenance(bridge):
    cfg = config()
    rule = cfg['rules'][0]
    with patch('toolbox.features.feishu_bridge.lark_json', side_effect=[
            response(message(reply_to='om_old_request')),
            response(message('om_old_request', sender={'id': rule['sender_id'], 'sender_type': 'user'}))]), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD):
        assert bridge._resolve_context(message(reply_to='om_parent'), rule, cfg)[0] == OTHER_THREAD
        assert bridge._known_context('om_old_request', rule, cfg) == OTHER_THREAD


@pytest.mark.parametrize('parent', [
    message(chat_id='oc_other'), message(deleted=True),
    message(sender={'id': 'cli_other', 'sender_type': 'app'}),
    message(sender={'id': 'ou_other', 'sender_type': 'user'}),
])
def test_untrusted_parent_cannot_select_context(bridge, parent):
    cfg = config()
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response(parent)), \
         patch('toolbox.features.feishu_bridge.find_source_thread') as find:
        with pytest.raises(RuntimeError):
            bridge._resolve_context(message(reply_to='om_parent'), cfg['rules'][0], cfg)
        find.assert_not_called()


def test_unknown_reply_never_falls_back_or_dispatches(bridge):
    cfg = config()
    source = event()
    incoming = message(source['message_id'], reply_to='om_parent', sender={'id': source['sender_id'], 'sender_type': 'user'})
    bridge._claim(source['message_id'], 'owner')
    with patch('toolbox.features.feishu_bridge.lark_json', side_effect=[response(incoming), response(message())]), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=None), \
         patch('toolbox.features.feishu_bridge.connect_codex') as connect, patch.object(bridge, '_blocked') as blocked:
        bridge._process(source, cfg['rules'][0], cfg)
        connect.assert_not_called()
        blocked.assert_called_once()
        assert '来源 context' in bridge.status()['recent'][0]['detail']


def test_routed_dispatch_maps_request_ack_and_answer(bridge):
    cfg = config()
    rule = cfg['rules'][0]
    source = event()
    incoming = message(source['message_id'], reply_to='om_parent', sender={'id': source['sender_id'], 'sender_type': 'user'})
    bridge._remember_context('om_parent', rule, cfg, OTHER_THREAD, 'test')
    bridge._claim(source['message_id'], rule['id'])
    with patch('toolbox.features.feishu_bridge.lark_json', side_effect=[response(incoming),
             response(message('om_answer', reply_to=source['message_id']))]), \
         patch('toolbox.features.feishu_bridge.connect_codex') as connect:
        bridge._process(source, rule, cfg, 'om_ack')
        connect.assert_called_once_with(OTHER_THREAD, launch=True)
        args = connect.return_value.tool.call_args.args[1]
        assert args['threadId'] == OTHER_THREAD
        assert 'om_parent' in args['prompt']
        for mid in (source['message_id'], 'om_ack', 'om_answer'):
            assert bridge._known_context(mid, rule, cfg) == OTHER_THREAD
        job = bridge.status()['recent'][0]
        assert job['target_thread_id'] == OTHER_THREAD and job['status'] == 'sent'


def test_context_is_scoped_by_chat_and_app(bridge):
    cfg = config()
    rule = cfg['rules'][0]
    bridge._remember_context('om_parent', rule, cfg, OTHER_THREAD, 'test')
    assert bridge._known_context('om_parent', {**rule, 'chat_id': 'oc_other'}, cfg) is None
    assert bridge._known_context('om_parent', rule, {**cfg, 'app_id': 'cli_other'}) is None


def test_reply_cycle_stops_without_dispatch(bridge):
    cfg = config()
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response(message(reply_to='om_parent'))):
        with pytest.raises(RuntimeError, match='循环'):
            bridge._resolve_context(message(reply_to='om_parent'), cfg['rules'][0], cfg)


def test_existing_database_migrates_without_losing_jobs(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'feishu-bridge.sqlite3') as db:
        db.execute('CREATE TABLE jobs (message_id TEXT PRIMARY KEY,rule_id TEXT,status TEXT,reply_id TEXT,updated_at INTEGER,detail TEXT)')
        db.execute("INSERT INTO jobs VALUES ('om_old','owner','sent','om_reply',1,'')")
    service = FeishuBridge(FakeApp(tmp_path))
    try:
        assert service.status()['recent'][0]['message_id'] == 'om_old'
        assert service.status()['recent'][0]['target_thread_id'] is None
        assert service._claim('om_new', 'owner')
    finally:
        service.close()


DSH_RULE = {'id': 'fish', 'name': '大肥鱼私聊', 'enabled': True, 'chat_type': 'p2p',
            'chat_id': 'oc_fish123', 'sender_id': 'ou_owner123', 'require_bot_mention': False,
            'target': 'dsh', 'message_types': ['text'], 'prompt': '回答 {message_id}', 'ack_text': ''}


def dsh_config(**changes):
    payload = {'enabled': False, 'app_id': 'cli_bot123', 'bot_open_id': 'ou_bot123',
               'dsh': {'enabled': True, 'profile': 'dafeiyu', 'app_id': 'cli_fish123',
                       'bot_open_id': 'ou_fish123'},
               'rules': [dict(DSH_RULE)]}
    payload.update(changes)
    return validate_config(payload)


def dsh_event(**changes):
    return {'type': 'im.message.receive_v1', 'chat_type': 'p2p', 'chat_id': 'oc_fish123',
            'sender_type': 'user', 'sender_id': 'ou_owner123', 'message_type': 'text',
            'message_id': 'om_fish123', 'mentions': [], **changes}


def test_dsh_config_defaults_and_validation():
    value = dsh_config()
    rule = value['rules'][0]
    assert rule['target'] == 'dsh' and rule['target_thread_id'] == ''
    assert value['dsh']['dsh_profile'] == 'headless'
    assert value['dsh']['permission_mode'] == 'danger-full-access'
    with pytest.raises(ValueError, match='Codex 对话'):
        validate_config({'enabled': True, 'app_id': 'cli_x', 'bot_open_id': 'ou_x',
                         'rules': [{'name': '普通规则', 'enabled': True, 'chat_type': 'p2p',
                                    'chat_id': 'oc_1', 'sender_id': 'ou_1', 'prompt': 'p'}]})
    with pytest.raises(ValueError, match='DSH 会话'):
        dsh_config(rules=[{**DSH_RULE, 'dsh_session': 'not-a-session'}])
    with pytest.raises(ValueError, match='DSH 路由'):
        dsh_config(dsh={'enabled': True, 'profile': 'dafeiyu'})
    with pytest.raises(ValueError, match='Codex 目标'):
        dsh_config(rules=[{**DSH_RULE, 'target': 'codex', 'dsh_session': 'session-abcdef12',
                           'target_thread_id': '01a0eb53-21a8-7522-bf57-19dd0c4daeca'}])


def test_rules_are_scoped_to_their_own_bot():
    value = dsh_config()
    assert matching_rule(dsh_event(), value, source='dsh')['id'] == 'fish'
    assert matching_rule(dsh_event(), value, source='codex') is None
    codex_only = config()
    assert matching_rule(event(), codex_only, source='codex')['id'] == 'owner'
    assert matching_rule(event(), codex_only, source='dsh') is None
    value['dsh']['bot_open_id'] = 'ou_fish123'
    assert matching_rule(dsh_event(sender_id='ou_fish123'), value, source='dsh') is None


def test_dsh_dispatch_answers_from_session(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    value = dsh_config()
    source = dsh_event()
    calls = []

    def fake_lark(*args, **kwargs):
        calls.append((args, kwargs))
        if '+messages-reply' in args:
            return {'ok': True, 'data': {'message_id': 'om_reply%02d' % len(calls)}}
        asked = args[args.index('--message-ids') + 1] if '--message-ids' in args else source['message_id']
        return {'ok': True, 'data': {'messages': [{
            'message_id': asked, 'chat_id': source['chat_id'],
            'sender': {'id': 'ou_owner123', 'sender_type': 'user'}, 'content': '帮我看看'}]}}
    try:
        with patch('toolbox.features.feishu_bridge.lark_json', side_effect=fake_lark), \
             patch.object(service, '_run_dsh', return_value={'ok': True,
                                                            'session': 'session-abc12345',
                                                            'answer': '看完了，没问题'}) as run:
            service._claim(source['message_id'], 'fish')
            service._process(source, value['rules'][0], value, '', 'dsh')
        assert run.call_args.args[1] == ''            # 首次没有会话，走默认会话
        job = service.status()['recent'][0]
        assert job['status'] == 'sent'
        assert service._dsh_session('oc_fish123') == 'session-abc12345'
        assert any('--markdown' in args for args, _ in calls)
        assert all(kwargs.get('profile') == 'dafeiyu' for _, kwargs in calls)
        with patch('toolbox.features.feishu_bridge.lark_json', side_effect=fake_lark), \
             patch.object(service, '_run_dsh', return_value={'ok': True, 'session': 'session-abc12345',
                                                            'answer': '继续'}) as again:
            service._claim('om_fish456', 'fish')
            service._process(dsh_event(message_id='om_fish456'), value['rules'][0], value, '', 'dsh')
        assert again.call_args.args[1] == 'session-abc12345'   # 同一会话继续
    finally:
        service.close()


def test_dsh_failure_reports_blocked(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    value = dsh_config()
    source = dsh_event()
    try:
        with patch('toolbox.features.feishu_bridge.lark_json', return_value={'ok': True, 'data': {'messages': [{
                'message_id': source['message_id'], 'chat_id': source['chat_id'],
                'sender': {'id': 'ou_owner123', 'sender_type': 'user'}, 'content': 'x'}]}}), \
             patch.object(service, '_run_dsh', return_value={'ok': False, 'error': 'DSH 会话超时'}), \
             patch.object(service, '_blocked_dsh') as blocked:
            service._claim(source['message_id'], 'fish')
            service._process(source, value['rules'][0], value, '', 'dsh')
        assert service.status()['recent'][0]['status'] == 'failed'
        blocked.assert_called_once()
    finally:
        service.close()


def test_run_dsh_parses_session_and_final(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    value = dsh_config()
    proc = Mock(returncode=0, stderr='', stdout='{"type":"session","sessionId":"session-xyz98765"}\n'
                                            '{"type":"text","text":"hi"}\n{"type":"final","text":"最终答案"}\n')
    try:
        with patch('toolbox.features.feishu_bridge.dsh_command', return_value='dsh'), \
             patch('toolbox.features.feishu_bridge.subprocess.run', return_value=proc) as runner:
            result = service._run_dsh('任务', None, value)
        assert result == {'ok': True, 'session': 'session-xyz98765', 'answer': '最终答案'}
        env = runner.call_args.kwargs['env']
        assert env['DSH_PERMISSION_MODE'] == 'danger-full-access'
        assert runner.call_args.args[0][:4] == ['dsh', '--profile', 'headless', '--json']
    finally:
        service.close()


def test_dsh_command_falls_back_to_install_directory(tmp_path):
    host = tmp_path / 'DSH Desktop' / 'host-commands' / 'desktop' / 'generations' / 'abc123' / 'bin'
    host.mkdir(parents=True)
    shim = host / 'dsh.cmd'
    shim.write_text('@echo off\n', encoding='utf-8')
    value = dsh_config()
    with patch('toolbox.features.feishu_bridge.shutil.which', return_value=None), \
         patch.dict('os.environ', {'APPDATA': str(tmp_path), 'LOCALAPPDATA': str(tmp_path)}):
        assert dsh_command(value) == str(shim)
        value['dsh']['command'] = r'C:\custom\dsh.cmd'
        assert dsh_command(value) == r'C:\custom\dsh.cmd'
    with patch('toolbox.features.feishu_bridge.shutil.which', return_value=None), \
         patch.dict('os.environ', {'APPDATA': str(tmp_path / 'empty'), 'LOCALAPPDATA': ''}):
        with pytest.raises(RuntimeError, match='dsh'):
            dsh_command({**value, 'dsh': {**value['dsh'], 'command': ''}})


def test_route_records_session_and_reply_continues_it(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    value = dsh_config()
    try:
        recorded = service.route_message({'message_id': 'om_asked01',
                                         'session_id': 'session-other12345',
                                         'chat_id': 'oc_fish123'})
        assert recorded['ok'] is True
        assert service._dsh_session_of_message('om_asked01') == 'session-other12345'
        assert service._dsh_session('oc_fish123') == 'session-other12345'

        reply_event = dsh_event(message_id='om_reply01')
        with patch('toolbox.features.feishu_bridge.lark_json') as lark:
            lark.side_effect = lambda *args, **kwargs: (
                {'ok': True, 'data': {'message_id': 'om_answer01'}} if '+messages-reply' in args
                else {'ok': True, 'data': {'messages': [{
                    'message_id': 'om_reply01', 'chat_id': 'oc_fish123', 'reply_to': 'om_asked01',
                    'sender': {'id': 'ou_owner123', 'sender_type': 'user'}, 'content': '继续'}]}})
            with patch.object(service, '_run_dsh', return_value={'ok': True,
                                                                'session': 'session-other12345',
                                                                'answer': '好'}) as run:
                service._claim('om_reply01', 'fish')
                service._process(reply_event, value['rules'][0], value, '', 'dsh')
        assert run.call_args.args[1] == 'session-other12345'
        # the answer itself is remembered too, so replying to it keeps the same session
        assert service._dsh_session_of_message('om_answer01') == 'session-other12345'
    finally:
        service.close()


def test_route_rejects_bad_ids(tmp_path):
    service = FeishuBridge(FakeApp(tmp_path))
    try:
        with pytest.raises(ValueError, match='消息 ID'):
            service.route_message({'message_id': 'nope', 'session_id': 'session-abc12345'})
        with pytest.raises(ValueError, match='DSH 会话'):
            service.route_message({'message_id': 'om_ok1', 'session_id': 'nope'})
        with pytest.raises(ValueError, match='chat_id'):
            service.route_message({'message_id': 'om_ok1', 'session_id': 'session-abc12345',
                                   'chat_id': 'bad'})
    finally:
        service.close()


def test_codex_executable_keeps_path_precedence(tmp_path):
    from toolbox.features.feishu_bridge import executable

    with patch('toolbox.features.feishu_bridge.shutil.which', return_value=r'C:\custom\codex.exe'), \
         patch('toolbox.features.feishu_bridge.discover_codex') as discover:
        assert executable('codex.exe') == r'C:\custom\codex.exe'
        discover.assert_not_called()


def test_codex_launch_finds_latest_desktop_cli_without_path(tmp_path):
    import os
    from toolbox.features.feishu_bridge import connect_codex

    root = tmp_path / 'OpenAI' / 'Codex' / 'bin'
    old = root / 'old' / 'codex.exe'
    latest = root / 'new' / 'codex.exe'
    for path, stamp in ((old, 100), (latest, 200)):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'fixture')
        os.utime(path, (stamp, stamp))
    client = Mock()
    client.call.return_value = {'tools': [{'name': 'send_message_to_thread'}]}
    with patch.dict('os.environ', {'LOCALAPPDATA': str(tmp_path), 'APPDATA': str(tmp_path / 'empty')}), \
         patch('toolbox.features.feishu_bridge.shutil.which', return_value=None), \
         patch('toolbox.features.feishu_bridge.app_pipes', side_effect=[[], ['fixture-pipe']]), \
         patch('toolbox.features.feishu_bridge.AppTools', return_value=client), \
         patch('toolbox.features.feishu_bridge.time.sleep'), \
         patch('toolbox.features.feishu_bridge.subprocess.Popen') as launch:
        assert connect_codex(OTHER_THREAD, launch=True) is client
        assert launch.call_args.args[0] == [str(latest), 'app']


def test_codex_executable_finds_npm_native_cli_without_path(tmp_path):
    from toolbox.features.feishu_bridge import executable

    native = (tmp_path / 'npm/node_modules/@openai/codex/node_modules/@openai/'
              'codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')
    native.parent.mkdir(parents=True)
    native.write_bytes(b'fixture')
    with patch.dict('os.environ', {'LOCALAPPDATA': str(tmp_path / 'empty'), 'APPDATA': str(tmp_path)}), \
         patch('toolbox.features.feishu_bridge.shutil.which', return_value=None):
        assert executable('codex.exe') == str(native)


@pytest.mark.parametrize('name, expected', [('codex.exe', 'Codex CLI'), ('node.exe', 'PATH'),
                                          ('lark-cli.cmd', '飞书 CLI')])
def test_missing_executable_reports_its_own_dependency(tmp_path, name, expected):
    from toolbox.features.feishu_bridge import executable

    with patch.dict('os.environ', {'LOCALAPPDATA': str(tmp_path), 'APPDATA': str(tmp_path)}), \
         patch('toolbox.features.feishu_bridge.shutil.which', return_value=None):
        with pytest.raises(RuntimeError, match=expected) as failure:
            executable(name)
        if name != 'lark-cli.cmd':
            assert '飞书 CLI' not in str(failure.value)


def test_app_pipes_discovers_modern_pipe_without_codex_environment():
    from toolbox.features.feishu_bridge import app_pipes

    with patch.dict('os.environ', {'CODEX_APP_TOOLS_PIPE_PATH': ''}), \
         patch('toolbox.features.feishu_bridge.os.listdir', return_value=[
             'codex-browser-use-legacy', 'unrelated', 'codex-app-tools-modern']):
        assert app_pipes() == [r'\\.\pipe\codex-app-tools-modern',
                               r'\\.\pipe\codex-browser-use-legacy']


def test_app_pipes_preserves_explicit_pipe_and_deduplicates():
    from toolbox.features.feishu_bridge import app_pipes

    preferred = r'\\.\pipe\codex-app-tools-preferred'
    with patch.dict('os.environ', {'CODEX_APP_TOOLS_PIPE_PATH': preferred}), \
         patch('toolbox.features.feishu_bridge.os.listdir', return_value=[
             'codex-app-tools-preferred', 'codex-app-tools-other']):
        assert app_pipes() == [preferred, r'\\.\pipe\codex-app-tools-other']
