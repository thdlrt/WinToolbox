#!/usr/bin/env python
"""Register "this bot message came from DSH session X" in WinToolbox's Feishu bridge.

Run this right after a DSH session sends a Feishu message through the toolbox-managed
bot, so the user's reply to that message is routed back to the same session.

  python feishu_route.py --message-id om_xxx            # session id from $DSH_SESSION_ID
  python feishu_route.py --message-id om_xxx --chat-id oc_xxx --session-id session-…
  python feishu_route.py --message-id om_xxx --db <path to feishu-bridge.sqlite3>

Stdlib only, safe to run from any Python 3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

MESSAGE_ID = re.compile(r'^om_[A-Za-z0-9_-]+$')
CHAT_ID = re.compile(r'^oc_[A-Za-z0-9_-]+$')
SESSION_ID = re.compile(r'^session-[A-Za-z0-9-]{8,}$')
DEFAULT_DB = Path(r'E:\code\other\WinToolbox\dist\WinToolbox-portable\data\feishu-bridge.sqlite3')


def find_db(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get('WINTOOLBOX_FEISHU_DB')
    if env:
        return Path(env)
    candidates = [DEFAULT_DB,
                  Path(r'C:\Users\caome\AppData\Local\WinToolbox\feishu-bridge.sqlite3')]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return DEFAULT_DB


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--message-id', required=True)
    parser.add_argument('--session-id', default=os.environ.get('DSH_SESSION_ID', ''))
    parser.add_argument('--chat-id', default='')
    parser.add_argument('--db', default=None)
    args = parser.parse_args()

    if not MESSAGE_ID.fullmatch(args.message_id):
        print(json.dumps({'ok': False, 'error': '消息 ID 无效'}, ensure_ascii=False))
        return 2
    if not SESSION_ID.fullmatch(args.session_id or ''):
        print(json.dumps({'ok': False, 'error': '会话 ID 无效（可用 --session-id 或设置 DSH_SESSION_ID）'},
                         ensure_ascii=False))
        return 2
    if args.chat_id and not CHAT_ID.fullmatch(args.chat_id):
        print(json.dumps({'ok': False, 'error': 'chat_id 无效'}, ensure_ascii=False))
        return 2

    db_path = find_db(args.db)
    if not db_path.is_file():
        print(json.dumps({'ok': False, 'error': f'未找到桥数据库：{db_path}'}, ensure_ascii=False))
        return 3
    try:
        with sqlite3.connect(db_path, timeout=30) as db:
            db.execute('CREATE TABLE IF NOT EXISTS dsh_outbound ('
                       'message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, chat_id TEXT NOT NULL, '
                       'created_at INTEGER NOT NULL)')
            db.execute('INSERT INTO dsh_outbound (message_id,session_id,chat_id,created_at) VALUES (?,?,?,?) '
                       'ON CONFLICT(message_id) DO UPDATE SET session_id=excluded.session_id, '
                       'chat_id=excluded.chat_id, created_at=excluded.created_at',
                       (args.message_id, args.session_id, args.chat_id, int(time.time())))
            if args.chat_id:
                db.execute('CREATE TABLE IF NOT EXISTS dsh_sessions ('
                           'chat_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at INTEGER NOT NULL)')
                db.execute('INSERT INTO dsh_sessions (chat_id,session_id,updated_at) VALUES (?,?,?) '
                           'ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id, '
                           'updated_at=excluded.updated_at',
                           (args.chat_id, args.session_id, int(time.time())))
            db.commit()
    except sqlite3.Error as exc:
        print(json.dumps({'ok': False, 'error': f'写入失败：{exc}'}, ensure_ascii=False))
        return 4
    print(json.dumps({'ok': True, 'db': str(db_path), 'message_id': args.message_id,
                      'session_id': args.session_id, 'chat_id': args.chat_id}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
