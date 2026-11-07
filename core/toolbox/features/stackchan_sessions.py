"""Merge independent local agent sources without treating one failure as both."""
from concurrent.futures import ThreadPoolExecutor
import copy
import time

from .stackchan_dsh import DshSessions
from .stackchan_panel import read_sessions


class AgentSessions:
    def __init__(self, codex=read_sessions, dsh=None):
        self.codex = codex
        self.dsh = dsh or DshSessions()
        self.cache = {}
        self.retry_at = {}

    def read(self, config):
        if self.dsh.port != config['dsh_port']:
            self.dsh.port = config['dsh_port']
            self.dsh.cookies.clear()
        readers = {'codex': lambda: self.codex(config['thread_id'])}
        if config['dsh_enabled']:
            readers['dsh'] = self.dsh.read
        sources, rows = {}, []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='stackchan-agent-source') as pool:
            work = {name: pool.submit(reader) for name, reader in readers.items()
                    if time.monotonic() >= self.retry_at.get(name, 0)}
            for name in readers:
                if name not in work:
                    sources[name] = {'available': False, 'error': f'{name} 会话源暂不可用，稍后重试'}
            for name, future in work.items():
                try:
                    value = future.result()
                    if not value.get('available') or value.get('error'):
                        raise RuntimeError('会话源尚未就绪')
                    self.cache[name] = copy.deepcopy(value)
                    self.retry_at.pop(name, None)
                except Exception as exc:
                    self.retry_at[name] = time.monotonic() + 60
                    hint = '请检查 DSH 和 Pocket 已开启及本机端口' if name == 'dsh' else '请检查 Codex 桌面端连接'
                    value = {**copy.deepcopy(self.cache.get(name, {})), 'error':
                             f'{hint}（{type(exc).__name__}），60秒后重试'}
                fresh = bool(value.get('available') and not value.get('error'))
                sources[name] = {k: value[k] for k in ('sampled_at', 'error') if k in value}
                sources[name]['available'] = fresh
                if not fresh:
                    continue
                for row in value.get('rows', []):
                    normalized = {**row, 'source': name, 'stale': not fresh}
                    if name == 'codex':
                        normalized['title'] = '[Codex] ' + row['title'][:70]
                    rows.append(normalized)
        rows.sort(key=lambda r: (bool(r.get('stale')), {'waiting': 0, 'error': 0, 'running': 1, 'idle': 2}[r['state']],
                                 -float(r['updated_at'] or 0)))
        return {'available': any(s['available'] for s in sources.values()), 'sampled_at': time.time(),
                'error': None if any(s['available'] for s in sources.values()) else '所有会话源暂不可用',
                'sources': sources, 'rows': rows, 'total': len(rows),
                'running': sum(r['state'] == 'running' and not r['stale'] for r in rows),
                'waiting': sum(r['state'] in ('waiting', 'error') and not r['stale'] for r in rows)}
