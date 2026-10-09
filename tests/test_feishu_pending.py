from datetime import datetime
from unittest.mock import patch

import pytest

from toolbox.features.feishu_bridge import FeishuBridge, QuestionRoutingNeedsChoice, render_prompt
from toolbox.features.feishu_pending import is_question, question_history
from test_feishu_bridge import FakeApp, OTHER_THREAD, config, event, message, response


NOW = datetime.fromisoformat('2026-10-06 08:40').timestamp()


def incoming(**patches):
    values = dict(chat_id='oc_group123', create_time='2026-10-06 08:40',
                  sender={'id': 'ou_member123', 'sender_type': 'user'},
                  mentions=[{'id': 'ou_bot123'}], message_position='100')
    return message('om_incoming', **{**values, **patches})


def question(mid='om_question', **patches):
    values = dict(chat_id='oc_group123', create_time='2026-10-06 08:00',
                  content='请确认塞赫梅拉对应 rabbit 吗？', message_position='90')
    return message(mid, **{**values, **patches})


@pytest.fixture
def service(tmp_path):
    bridge = FeishuBridge(FakeApp(tmp_path))
    yield bridge
    bridge.close()


@pytest.mark.parametrize('text,expected', [
    ('请按图1–5回复对应角色名，我再据此整理。', True),
    ('塞赫梅拉就是 rabbit 吗？', True),
    ('请提供需要使用的图片。', True),
    ('构建完成，所有文件已经验证。', False),
    ('为什么要保留旧版本？因为可以回滚。', False),
    ('如需重新构建，请告诉我。', False),
    ('塔菲思考中，喵～', False),
    ('【普通提醒】定时打包已完成：cs50。下载后请确认路径；发现问题请提供截图。', False),
    ('塔菲喵把 WebGL 试玩版打包好啦，喵～  如有问题，请回复并提供截图。', False),
    ('构建完成，但资源有疑问：塞赫梅拉对应 rabbit 吗？', True),
])
def test_question_classification(text, expected):
    assert is_question({'content': text}) is expected


def test_mention_returns_unique_waiting_source_instead_of_default(service):
    cfg = config()
    rule = cfg['rules'][1]
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question())), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD):
        assert service._resolve_context(incoming(), rule, cfg) == (OTHER_THREAD, 'om_question')


def test_no_waiting_question_keeps_default_and_disabled_window_does_not_scan(service):
    cfg = config()
    rule = cfg['rules'][1]
    with patch('toolbox.features.feishu_bridge.lark_json', return_value=response()) as lark:
        assert service._resolve_context(incoming(), rule, cfg) == (rule['target_thread_id'], '')
        lark.reset_mock()
        assert service._resolve_context(incoming(), {**rule, 'pending_question_hours': 0}, cfg) == (rule['target_thread_id'], '')
        lark.assert_not_called()


def test_explicit_reply_wins_even_when_pending_sources_are_ambiguous(service):
    cfg = config()
    rule = cfg['rules'][1]
    service._remember_context('om_direct', rule, cfg, OTHER_THREAD, 'test')
    with patch('toolbox.features.feishu_bridge.lark_json') as lark:
        assert service._resolve_context(incoming(reply_to='om_direct'), rule, cfg) == (OTHER_THREAD, 'om_direct')
        lark.assert_not_called()


def test_different_pending_chats_require_choice_but_same_chat_can_continue(service):
    cfg = config()
    rule = cfg['rules'][1]
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question(), question('om_second'))), \
         patch('toolbox.features.feishu_bridge.find_source_thread', side_effect=[OTHER_THREAD, rule['target_thread_id']]):
        with pytest.raises(QuestionRoutingNeedsChoice, match='不同 Codex'):
            service._resolve_context(incoming(), rule, cfg)
    # Both questions mapping to one chat is a valid unambiguous destination.
    with service.lock:
        service.db.execute('UPDATE pending_questions SET thread_id=?', (OTHER_THREAD,))
        service.db.commit()
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question(), question('om_second'))):
        # Existing immutable message maps remain authoritative; use a fresh
        # question pair rather than changing them to simulate a new sender.
        service.db.execute('DELETE FROM pending_questions')
        service.db.execute('DELETE FROM message_contexts')
        service.db.commit()
        with patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD):
            assert service._resolve_context(incoming(), rule, cfg)[0] == OTHER_THREAD


def test_question_targeting_sender_wins_over_other_pending_questions(service):
    cfg = config()
    rule = cfg['rules'][1]
    questions = [question(), question('om_targeted', mentions=[{'id': 'ou_member123'}]),
                 question('om_somebody_else', mentions=[{'id': 'ou_other'}])]
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(*questions)), \
         patch('toolbox.features.feishu_bridge.find_source_thread', side_effect=[rule['target_thread_id'], OTHER_THREAD, rule['target_thread_id']]):
        assert service._resolve_context(incoming(), rule, cfg) == (OTHER_THREAD, 'om_targeted')


def test_unknown_question_source_never_falls_back(service):
    cfg = config()
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question())), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=None):
        with pytest.raises(RuntimeError, match='无法核验'):
            service._resolve_context(incoming(), cfg['rules'][1], cfg)


def test_answered_questions_acknowledgements_foreign_bots_expiry_future_and_chat_are_excluded(service):
    cfg = config()
    rule = cfg['rules'][1]
    answer = message('om_answer', chat_id=rule['chat_id'], reply_to='om_question', create_time='2026-10-06 08:10',
                     sender={'id': 'ou_member123', 'sender_type': 'user'}, message_position='91')
    followup = message('om_final', chat_id=rule['chat_id'], reply_to='om_answer', content='收到，已确认。',
                       create_time='2026-10-06 08:15', message_position='92')
    history = [question(), answer, followup,
               question('om_ack', content=cfg['ack_text']),
               question('om_foreign', sender={'id': 'cli_other', 'sender_type': 'app'}),
               question('om_deleted', deleted=True),
               question('om_old', create_time='2026-09-01 00:00', message_position='1'),
               question('om_future', message_position='101'), question('om_other_chat', chat_id='oc_other')]
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(*history)), \
         patch('toolbox.features.feishu_bridge.find_source_thread') as find:
        assert service._resolve_context(incoming(), rule, cfg) == (rule['target_thread_id'], '')
        find.assert_not_called()


def test_human_answer_without_bot_followup_is_still_pending_and_followup_question_replaces_old():
    cfg = config()
    rule = cfg['rules'][1]
    reply = message('om_answer', chat_id=rule['chat_id'], reply_to='om_question', create_time='2026-10-06 08:10',
                    sender={'id': 'ou_member123', 'sender_type': 'user'}, message_position='91')
    questions, answered = question_history([question(), reply], rule, cfg, incoming(), NOW-86400)
    assert 'om_question' in questions and not answered
    new_question = question('om_clarify', reply_to='om_answer', message_position='92')
    questions, answered = question_history([question(), reply, new_question], rule, cfg, incoming(), NOW-86400)
    assert answered == {'om_question'} and 'om_clarify' in questions


def test_pending_state_survives_restart_and_closes_only_after_final_reply(tmp_path):
    cfg = config()
    rule = cfg['rules'][1]
    service = FeishuBridge(FakeApp(tmp_path))
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question())), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD):
        service._resolve_context(incoming(), rule, cfg)
    service._track_question_answer('om_question', 'om_incoming', rule, cfg)
    service.close()
    service = FeishuBridge(FakeApp(tmp_path))
    try:
        assert service.db.execute('SELECT state,last_answer_id FROM pending_questions').fetchone() == ('open', 'om_incoming')
        service._track_question_answer('om_question', 'om_incoming', rule, cfg, completed=True)
        with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
             patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question())):
            assert service._resolve_context(incoming(), rule, cfg)[0] == rule['target_thread_id']
    finally:
        service.close()


def test_ambiguous_mention_asks_for_reply_without_ack_or_dispatch(service):
    cfg = config()
    rule = cfg['rules'][1]
    source = event(chat_type='group', chat_id=rule['chat_id'], sender_id='ou_member123')
    source['message_id'] = 'om_incoming'
    service._claim(source['message_id'], rule['id'])
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', side_effect=[response(incoming()), response(question(), question('om_second')),
                                                                       {'ok': True, 'data': {'message_id': 'om_choice'}}]) as lark, \
         patch('toolbox.features.feishu_bridge.find_source_thread', side_effect=[OTHER_THREAD, rule['target_thread_id']]), \
         patch('toolbox.features.feishu_bridge.connect_codex') as connect:
        service._process(source, rule, cfg)
        connect.assert_not_called()
        assert service.status()['recent'][0]['status'] == 'needs_context'
        assert any(str(arg).endswith(':context-choice') for arg in lark.call_args.args)
        assert all(not any(str(arg).endswith(':thinking') for arg in call.args) for call in lark.call_args_list)


def test_pending_prompt_points_to_original_question_and_keeps_group_scope():
    cfg = config()
    prompt = render_prompt(cfg['rules'][1], event(context_source_message_id='om_question', context_route_kind='pending_question'), '')
    assert '原提问消息 ID：om_question' in prompt and '不代表主人私聊授权' in prompt


def test_restart_recovers_completion_of_unthreaded_mention_answer(tmp_path):
    cfg = config()
    rule = cfg['rules'][1]
    service = FeishuBridge(FakeApp(tmp_path))
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question())), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD):
        service._resolve_context(incoming(), rule, cfg)
    service._track_question_answer('om_question', 'om_incoming', rule, cfg)
    service.close()
    service = FeishuBridge(FakeApp(tmp_path))
    try:
        final = message('om_final', chat_id=rule['chat_id'], reply_to='om_incoming', content='收到，已确认。',
                        create_time='2026-10-06 08:45', message_position='101')
        next_message = incoming(message_id='om_next', create_time='2026-10-06 08:50', message_position='110')
        with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW+600), \
             patch('toolbox.features.feishu_bridge.lark_json', return_value=response(question(), incoming(), final)):
            assert service._resolve_context(next_message, rule, cfg) == (rule['target_thread_id'], '')
    finally:
        service.close()


def test_unthreaded_pending_dispatch_returns_to_source_and_marks_handled_after_final(service):
    cfg = config()
    rule = cfg['rules'][1]
    source = event(chat_type='group', chat_id=rule['chat_id'], sender_id='ou_member123', message_id='om_incoming')
    service._claim(source['message_id'], rule['id'])
    final = message('om_final', chat_id=rule['chat_id'], reply_to='om_incoming', content='收到，已确认。')
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', side_effect=[response(incoming()), response(question()),
                                                                      {'ok': True, 'data': {'message_id': 'om_ack'}}, response(final)]), \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD), \
         patch('toolbox.features.feishu_bridge.connect_codex') as connect:
        service._process(source, rule, cfg)
        connect.assert_called_once_with(OTHER_THREAD, launch=True)
        assert '待答提问' in connect.return_value.tool.call_args.args[1]['prompt']
        assert service.db.execute('SELECT state FROM pending_questions').fetchone()[0] == 'answered'
        assert service.status()['recent'][0]['status'] == 'sent'


def test_paging_finds_older_question_and_incomplete_history_cannot_use_default(service):
    cfg = config()
    rule = cfg['rules'][1]
    first = {'ok': True, 'data': {'messages': [message('om_recent', chat_id=rule['chat_id'],
                                                    create_time='2026-10-06 08:10')], 'page_token': 'next'}}
    with patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', side_effect=[first, response(question())]) as lark, \
         patch('toolbox.features.feishu_bridge.find_source_thread', return_value=OTHER_THREAD):
        assert service._resolve_context(incoming(), rule, cfg)[0] == OTHER_THREAD
        assert '--page-token' in lark.call_args.args
    with patch('toolbox.features.feishu_bridge.MAX_QUESTION_PAGES', 1), \
         patch('toolbox.features.feishu_bridge.time.time', return_value=NOW), \
         patch('toolbox.features.feishu_bridge.lark_json', return_value=first):
        with pytest.raises(QuestionRoutingNeedsChoice, match='未能完整读取'):
            service._resolve_context(incoming(), rule, cfg)
