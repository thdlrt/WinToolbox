"""Silent agent waiting episodes and verified DSH turn ends; never answer/approve."""
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

from ..settings import atomic_json

LOG = logging.getLogger(__name__)


class WaitingNotices:
    def __init__(self, data_dir, enabled, *, deliver=None, clock=time.time):
        self.path = Path(data_dir) / 'stackchan-attention.json'
        self.enabled, self.deliver, self.clock = enabled, deliver or self._deliver, clock
        self.lock = threading.Lock()
        self.worker = None
        self.outcome = None
        try:
            saved = json.loads(self.path.read_text('utf-8'))
            self.episodes = saved['episodes']
            if not isinstance(self.episodes, dict):
                raise ValueError('invalid waiting episodes')
            self.outcome = saved.get('last_outcome')
            self.completed = saved.get('completed', {})
        except (OSError, ValueError, KeyError, TypeError):
            self.episodes = {}
            self.completed = {}

    def status(self):
        with self.lock:
            return {'waiting_episodes': len(self.episodes), 'last_outcome': self.outcome,
                    'source': 'Codex waiting/error + DSH questions, approval audit and verified turn/end'}

    def observe(self, snapshot, config):
        if not self.enabled() or not snapshot.get('available') or snapshot.get('error'):
            return
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            changed, pending = False, []
            for row in snapshot.get('rows', []):
                if row.get('stale'):
                    continue
                thread = row['id']
                completion = row.get('completion') or {}
                if completion.get('kind') == 'done' and self.completed.get(thread) != completion['seq']:
                    self.completed[thread] = completion['seq']
                    changed = True
                    # Distinct turns can finish within 30s. The durable sequence,
                    # not a generic recent green notice, identifies this event.
                    pending.append({**row, 'notice_kind': 'done'})
                if row['state'] not in ('waiting', 'error'):
                    if thread in self.episodes:
                        del self.episodes[thread]
                        changed = True
                    continue
                if thread in self.episodes:
                    continue  # updatedAt changes do not create another waiting episode.
                now = self.clock()
                self.episodes[thread] = {'first_seen': now, 'state': row['state']}
                changed = True
                if not self._already_notified(config, thread, now):
                    pending.append(row)
            if changed:
                atomic_json(self.path, {'episodes': self.episodes, 'completed': self.completed})
            if pending:
                self.worker = threading.Thread(target=self._run, args=(pending, dict(config)),
                                               name='stackchan-waiting-notice', daemon=True)
                self.worker.start()

    def _already_notified(self, config, thread, now, kind='problem'):
        path = Path(config.get('companion_dir') or '') / 'logs/task-notices.sqlite3'
        if not path.is_file():
            return False
        try:
            with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True, timeout=.2) as db:
                return db.execute("SELECT 1 FROM notices WHERE thread_id=? AND kind=? "
                                  "AND outcome='acknowledged' AND at>=? LIMIT 1", (thread, kind, now - 30)).fetchone() is not None
        except sqlite3.Error:
            return False

    def _run(self, rows, config):
        try:
            if not self.enabled():
                return
            # A completion and a problem never share a green card.
            groups = [[r for r in rows if r.get('notice_kind', 'problem') == kind] for kind in ('done', 'problem')]
            for group in groups:
                if group:
                    outcome = self.deliver(group, config)
        except Exception as exc:
            LOG.warning('StackChan 等待提醒未送达：%s', type(exc).__name__)
            outcome = 'unavailable'
        with self.lock:
            self.outcome = outcome
            atomic_json(self.path, {'episodes': self.episodes, 'completed': self.completed, 'last_outcome': outcome})

    def _deliver(self, rows, config):
        root = Path(config['companion_dir'])
        connection = json.loads((root / 'connection.json').read_text('utf-8'))
        if (connection['host'], connection['port']) != (config['host'], config['port']):
            raise ValueError('提醒入口与机器人设置不一致')
        title = rows[0]['title'] if len(rows) == 1 else f"{len(rows)}个任务等待处理"
        kind = rows[0].get('notice_kind', 'problem')
        summary = '等待你在PC选择、补充输入或确认权限，请打开对应会话处理。'
        if len(rows) > 1:
            summary = '、'.join(r['title'][:14] for r in rows[:3]) + '：请回PC处理待选项或审批。'
        if rows[0]['state'] == 'error':
            summary = '会话报告异常，请在PC查看具体错误并处理。'
        if kind == 'done':
            title = rows[0]['title'] if len(rows) == 1 else f'{len(rows)}个DSH会话本轮完成'
            summary = rows[0]['completion']['summary'] if len(rows) == 1 else '、'.join(r['title'][:14] for r in rows[:3]) + '：本轮执行结束。'
        key = [(r['id'], r['completion']['seq'] if kind == 'done' else self.episodes[r['id']]['first_seen']) for r in rows]
        event = 'waiting-' + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-X', 'utf8', str(root / 'task_notify.py'),
                                 '--status', kind, '--task', title[:80], '--summary', summary[:60],
                                 '--thread-id', rows[0]['id'], '--event-id', event, '--seconds', '12' if kind == 'done' else '120'],
                                capture_output=True, text=True, encoding='utf-8', env=env, timeout=22,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return json.loads(result.stdout).get('status', 'unavailable')
