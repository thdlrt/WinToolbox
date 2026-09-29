import json
import sqlite3

import pytest

from toolbox.features.feishu_sources import find_source_thread, sent_in_rollout

THREAD = '01a0eb1f-0b44-7f01-9b3f-dec88469a707'
OTHER = '01a0eb53-21a8-7522-bf57-19dd0c4daeca'


def rollout(path, thread=THREAD, command=None, receipt=None, output_kind='custom_tool_call_output', start='2026-09-29T00:00:00Z'):
    rows = [
        {'type': 'session_meta', 'payload': {'id': thread, 'timestamp': start}},
        {'timestamp': '2026-09-29T00:01:00Z', 'payload': {'type': 'custom_tool_call', 'call_id': 'call_send',
          'input': command or 'lark-cli im +messages-send --as bot --chat-id oc_chat --text hello'}},
        {'timestamp': '2026-09-29T00:02:00Z', 'payload': {'type': output_kind, 'call_id': 'call_send',
          'output': receipt if receipt is not None else [{'type': 'input_text', 'text': json.dumps({'output': json.dumps({'ok': True, 'data': {'message_id': 'om_sent'}})})}]}},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    return path


def test_wrapped_success_and_projected_receipts(tmp_path):
    path = rollout(tmp_path / 'rollout.jsonl')
    assert sent_in_rollout(path, THREAD, 'om_sent', 'oc_chat')


def test_yielded_exec_receipt_is_matched_to_its_wait(tmp_path):
    path = rollout(tmp_path / 'rollout.jsonl', receipt='Script running with cell ID 123')
    with path.open('a', encoding='utf-8') as stream:
        for payload in (
            {'type': 'function_call', 'name': 'functions.wait', 'call_id': 'wait', 'arguments': '{"cell_id":"123"}'},
            {'type': 'function_call_output', 'call_id': 'wait', 'output': json.dumps({'ok': True, 'data': {'message_id': 'om_sent'}})},
        ):
            stream.write('\n' + json.dumps({'timestamp': '2026-09-29T00:03:00Z', 'payload': payload}))
    assert sent_in_rollout(path, THREAD, 'om_sent', 'oc_chat')
    path = rollout(path, receipt=json.dumps({'ok': True, 'message_id': 'om_sent'}).replace('"', '\\"'))
    assert sent_in_rollout(path, THREAD, 'om_sent', 'oc_chat')


@pytest.mark.parametrize('kwargs', [
    {'command': 'lark-cli im +messages-mget --as bot --message-ids om_sent'},
    {'command': 'lark-cli im +messages-send --as bot --chat-id oc_other --text hi'},
    {'command': 'lark-cli im +messages-send --as bot --chat-id oc_chat --dry-run'},
    {'receipt': {'ok': False, 'data': {'message_id': 'om_sent'}}},
    {'output_kind': 'message'},
    {'start': '2026-09-29T00:03:00Z'},
])
def test_unproven_or_inherited_sends_are_ignored(tmp_path, kwargs):
    assert not sent_in_rollout(rollout(tmp_path / 'rollout.jsonl', **kwargs), THREAD, 'om_sent', 'oc_chat')


def test_local_index_lookup_is_unique_and_read_only(tmp_path):
    path = rollout(tmp_path / 'rollout.jsonl')
    index = tmp_path / 'state_5.sqlite'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE threads (id TEXT,rollout_path TEXT,updated_at INTEGER)')
        db.execute('INSERT INTO threads VALUES (?,?,?)', (THREAD, str(path), 1790640120))
    msg = {'message_id': 'om_sent', 'chat_id': 'oc_chat', 'create_time': '2026-09-29 00:02'}
    original = index.read_bytes()
    assert find_source_thread(msg, tmp_path) == THREAD
    assert index.read_bytes() == original
    other = rollout(tmp_path / 'other.jsonl', thread=OTHER)
    with sqlite3.connect(index) as db:
        db.execute('INSERT INTO threads VALUES (?,?,?)', (OTHER, str(other), 1790640120))
    with pytest.raises(RuntimeError, match='多个来源'):
        find_source_thread(msg, tmp_path)
