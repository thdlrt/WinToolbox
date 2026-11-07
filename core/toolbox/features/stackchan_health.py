"""Observe actual robot uptime; a timeout alone is never counted as a restart."""
import copy
import json
import math
from pathlib import Path
import time

from ..settings import atomic_json


class RobotHealth:
    def __init__(self, data_dir, clock=time.time):
        self.path = Path(data_dir) / 'stackchan-health.json'
        self.clock = clock
        self.written = 0
        self.value = {'supported': False, 'observed_restarts': 0, 'events': []}
        try:
            saved = json.loads(self.path.read_text('utf-8'))
            if (isinstance(saved, dict) and isinstance(saved.get('uptime_seconds'), (int, float)) and
                isinstance(saved.get('events'), list) and type(saved.get('observed_restarts')) is int):
                self.value = saved
        except (OSError, ValueError):
            pass

    def observe(self, answer, config):
        uptime = answer.get('uptime_seconds')
        if isinstance(uptime, bool) or not isinstance(uptime, (int, float)) or not math.isfinite(uptime) or uptime < 0:
            return self.snapshot()
        target = f"{config.get('host')}:{config.get('port', 8080)}"
        if self.value.get('target') != target:
            self.value = {'supported': True, 'target': target, 'observed_restarts': 0, 'events': []}
        previous = self.value.get('uptime_seconds')
        # Time.ticks converted to unsigned seconds wraps after about49.7 days.
        wrapped = previous is not None and previous > 4294900 and uptime < 120
        restarted = previous is not None and uptime < previous - 2 and not wrapped
        now = self.clock()
        if restarted:
            runtime = answer.get('runtime') or {}
            event = {'observed_at': now, 'previous_uptime_seconds': previous, 'uptime_seconds': uptime,
                     'reason': 'uptime-decreased', 'reset_cause': runtime.get('reset_cause') or 'unknown',
                     'previous_page': self.value.get('page'), 'previous_screen': self.value.get('screen'),
                     'previous_diagnostics': self.value.get('diagnostics', {})}
            self.value['observed_restarts'] += 1
            self.value['events'] = (self.value['events'] + [event])[-20:]
        self.value.update(supported=True, observed_at=now, uptime_seconds=uptime,
                          page=answer.get('page'), screen=answer.get('screen'))
        interaction = answer.get('interaction') or {}
        pending = interaction.get('pending') or {}
        runtime = answer.get('runtime') or {}
        self.value['diagnostics'] = {**{k: answer[k] for k in ('draw_count', 'draw_ms', 'max_draw_ms', 'decode_ms', 'receive_ms', 'error') if k in answer},
                                    'motion_busy': bool(interaction.get('busy')), 'motion_result': interaction.get('result'),
                                    'pending_kind': pending.get('kind'),
                                    'runtime': {k: runtime[k] for k in ('build', 'boot_id', 'reset_cause', 'chunk_used', 'system_free') if k in runtime}}
        if restarted or now - self.written >= 30:
            try:
                atomic_json(self.path, self.value)
                self.written = now
            except OSError:
                pass
        return self.snapshot()

    def snapshot(self):
        return copy.deepcopy(self.value)
