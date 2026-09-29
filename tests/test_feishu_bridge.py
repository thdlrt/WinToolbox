"""Feishu listener authorization and configuration tests."""
from unittest.mock import Mock, patch

import pytest

from toolbox.features.feishu_bridge import FeishuBridge, matching_rule, render_prompt, validate_config


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
    with patch('toolbox.features.feishu_bridge.lark_json') as lark:
        assert bridge._resolve_context(message(reply_to='om_parent'), cfg['rules'][1], cfg)[0] == cfg['rules'][1]['target_thread_id']
        lark.assert_not_called()


def test_known_reply_context_survives_restart_and_default_change(tmp_path):
    cfg = config()
    rule = cfg['rules'][0]
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
