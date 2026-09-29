"""Recover outgoing-message provenance from local Codex execution receipts.

This reads Codex's local index and rollouts; it never treats quoted message text,
assistant summaries or message IDs in read results as evidence of a send.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path


def receipt_objects(value, depth=0):
    if depth > 10:
        return
    if isinstance(value, dict):
        yield value
        # Only unwrap execution envelopes, never sent message bodies.
        for key in ('output', 'text', 'result', 'value'):
            if key in value:
                yield from receipt_objects(value[key], depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from receipt_objects(item, depth + 1)
    elif isinstance(value, str):
        decoder = json.JSONDecoder()
        cursor = 0
        found = False
        while cursor < len(value):
            start = value.find('{', cursor)
            if start < 0:
                break
            try:
                obj, end = decoder.raw_decode(value, start)
            except ValueError:
                cursor = start + 1
                continue
            found = True
            yield from receipt_objects(obj, depth + 1)
            cursor = end
        if not found and '\\"' in value:
            yield from receipt_objects(value.replace('\\"', '"').replace('\\r', '\r').replace('\\n', '\n'), depth + 1)


def sent_in_rollout(path, thread_id, message_id, chat_id):
    calls = set()
    pending_cells = set()
    pending_sessions = set()
    with Path(path).open(encoding='utf-8') as stream:
        meta = json.loads(next(stream)).get('payload', {})
        if meta.get('id') != thread_id:
            return False
        started = meta.get('timestamp', '')
        for line in stream:
            # Avoid decoding large unrelated history records.
            if not any(token in line for token in ('function_call', 'custom_tool_call')):
                continue
            row = json.loads(line)
            if row.get('timestamp', '') < started:
                continue  # A fork's inherited history is not its own send.
            item = row.get('payload', {})
            kind = item.get('type')
            call_id = item.get('call_id')
            if kind in ('function_call', 'custom_tool_call'):
                command = item.get('arguments', item.get('input', ''))
                if not isinstance(command, str):
                    continue
                name = item.get('name', '').split('.')[-1]
                if name in ('wait', 'write_stdin'):
                    try:
                        arguments = json.loads(command)
                    except ValueError:
                        arguments = {}
                    if (str(arguments.get('cell_id', '')) in pending_cells
                            or str(arguments.get('session_id', '')) in pending_sessions):
                        calls.add(call_id)
                send = '+messages-send' in command and chat_id in command
                reply = '+messages-reply' in command
                if ('lark-cli' in command and (send or reply) and '--as bot' in command
                        and '--dry-run' not in command
                        and not any(read in command for read in ('+messages-mget', '+chat-messages-list', '+messages-search'))):
                    calls.add(call_id)
            elif kind in ('function_call_output', 'custom_tool_call_output') and call_id in calls:
                output_text = json.dumps(item.get('output'), ensure_ascii=False)
                pending_cells.update(re.findall(r'Script running with cell ID ([A-Za-z0-9_-]+)', output_text))
                for result in receipt_objects(item.get('output')):
                    if result.get('session_id') is not None:
                        pending_sessions.add(str(result['session_id']))
                    if result.get('ok') is True:
                        data = result.get('data', result)
                        if isinstance(data, dict) and data.get('message_id') == message_id:
                            return True
    return False


def find_source_thread(message, codex_home=None):
    """Return a unique, verified local sender thread, or no match.

    Database/schema or missing-history errors fail closed at the routing layer.
    The creation time narrows candidates but never establishes provenance.
    """
    root = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    indexes = sorted(root.glob('state_*.sqlite'), key=lambda p: int(re.search(r'(\d+)\.sqlite$', p.name)[1]), reverse=True)
    if not indexes:
        return None
    created = message.get('create_time', '')
    try:
        since = int(datetime.fromisoformat(str(created)).timestamp()) - 86400
    except ValueError:
        since = 0
    with sqlite3.connect(indexes[0].as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        candidates = db.execute('SELECT id,rollout_path FROM threads WHERE updated_at>=?', (since,)).fetchall()
    matches = set()
    for thread_id, path in candidates:
        try:
            if sent_in_rollout(path, thread_id, message['message_id'], message['chat_id']):
                matches.add(thread_id)
        except (OSError, ValueError, StopIteration):
            continue
    if len(matches) > 1:
        raise RuntimeError('原消息存在多个来源 context，无法唯一确定')
    return next(iter(matches), None)
