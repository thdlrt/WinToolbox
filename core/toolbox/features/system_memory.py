"""Built-in memory telemetry, settings and in-process automatic cleaning."""
import contextlib
import ctypes
from ctypes import wintypes as w
import json
import os
import threading
import time

from ..settings import atomic_json
from .memory_worker import commands

_clean_lock = threading.Lock()
DEFAULTS = {'mode': 'default', 'auto_enabled': False, 'auto_mode': 'default',
            'threshold_enabled': True, 'threshold_percent': 80,
            'interval_enabled': False, 'interval_minutes': 30, 'cooldown_minutes': 5}


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('内存清理设置包含无效字段')
    result = {**DEFAULTS, **value}
    for name in ('mode', 'auto_mode'):
        commands(result[name])
    for name in ('auto_enabled', 'threshold_enabled', 'interval_enabled'):
        if type(result[name]) is not bool:
            raise ValueError('自动清理开关必须为布尔值')
    for name, minimum, maximum in (('threshold_percent', 50, 99), ('interval_minutes', 1, 1440), ('cooldown_minutes', 1, 1440)):
        if type(result[name]) is not int or not minimum <= result[name] <= maximum:
            raise ValueError(f'{name} 必须为 {minimum}–{maximum} 的整数')
    if result['auto_enabled'] and not (result['threshold_enabled'] or result['interval_enabled']):
        raise ValueError('启用自动清理时，请至少选择内存阈值或定时间隔')
    return result


def snapshot():
    if os.name != 'nt': raise ValueError('内存悬浮球仅支持 Windows')
    class Memory(ctypes.Structure):
        _fields_ = [('length', w.DWORD), ('load', w.DWORD)] + [(key, ctypes.c_ulonglong) for key in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
    value = Memory(); value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)): raise ctypes.WinError()
    return {'percent': value.load, 'total': value.total, 'available': value.available, 'used': value.total - value.available,
            'commit_total': value.page_total, 'commit_used': value.page_total - value.page_available}



def clean(job, session):
    if not _clean_lock.acquire(blocking=False):
        raise ValueError('内存清理正在进行，请稍候')
    try:
        job.check_cancelled()
        mode = job.params.get('mode', 'default')
        commands(mode)
        before = snapshot()
        if job.params.get('automatic') is True:
            result = session.clean(mode, job.progress, allow_install=False)
        else:
            result = session.clean(mode, job.progress)
        after = snapshot()
        return {**result, 'before': before, 'after': after,
                'available_change': after['available'] - before['available'],
                'message': '清理完成；可用内存变化以实时读数为准'}
    finally:
        _clean_lock.release()


class MemorySettings:
    def __init__(self, app):
        self.app = app
        self.path = app.data_dir / 'memory-cleaner.json'
        self.lock = threading.RLock()
        self.automation = None

    def values(self):
        with self.lock:
            value = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {}
            return validate_settings(value)

    def get(self, _=None):
        value = self.values()
        return {**value, 'automation': self.automation.status() if self.automation else {
            'running': False, 'paused_reason': None, 'last_run': None,
            'last_trigger': None, 'last_error': None, 'next_interval_at': None}}

    def save(self, params):
        if not isinstance(params, dict) or set(params) - set(DEFAULTS):
            raise ValueError('内存清理设置包含无效字段')
        with self.lock:
            value = validate_settings({**self.values(), **params})
            atomic_json(self.path, value)
        if self.automation:
            self.automation.wake()
        result = self.get()
        if hasattr(self.app, 'emit'):
            self.app.emit('memory.settings.changed', **result)
        return result

    def submit(self, params):
        mode = params.get('mode', self.values()['mode'])
        commands(mode)
        return self.app.jobs.submit('memory.clean', {'mode': mode})


class MemoryAutomation:
    """Monotonic scheduling, threshold hysteresis and a shared clean-job gate."""
    def __init__(self, app, settings, session, *, clock=time.monotonic, wall=time.time, reader=snapshot):
        self.app, self.settings, self.session = app, settings, session
        self.clock, self.wall, self.reader = clock, wall, reader
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.changed = threading.Event()
        self.thread = None
        self.last_config = None
        self.interval_anchor = clock()
        self.last_attempt = None
        self.armed = True
        self.job_id = None
        self.state = {'running': False, 'paused_reason': None, 'last_run': None,
                      'last_trigger': None, 'last_error': None, 'next_interval_at': None}
        settings.automation = self

    def status(self):
        with self.lock:
            return dict(self.state)

    def wake(self):
        self.changed.set()

    def busy(self):
        with getattr(self.app.jobs, 'lock', contextlib.nullcontext()):
            return any(getattr(job, 'record', {}).get('tool') == 'memory.clean'
                       for job in list(getattr(self.app.jobs, 'active', {}).values()))

    def started(self, params):
        with self.lock:
            self.state.update(running=True, last_trigger=params.get('trigger', 'manual'), paused_reason=None)
            self.last_attempt = self.clock()

    def finished(self, error=None):
        with self.lock:
            self.interval_anchor = self.clock()
            self.last_attempt = self.clock()
            self.state.update(running=False, last_error=error)
            if error is None:
                self.state['last_run'] = self.wall()
            self.job_id = None
        self.wake()

    def tick(self):
        if self.stop.is_set():
            return None
        value = self.settings.values()
        now = self.clock()
        with self.lock:
            old = self.last_config
            if old is None or any(old[key] != value[key] for key in ('auto_enabled', 'interval_enabled', 'interval_minutes')):
                self.interval_anchor = now
            if old is None or any(old[key] != value[key] for key in ('auto_enabled', 'threshold_enabled', 'threshold_percent')):
                self.armed = True
            self.last_config = value
            due = self.interval_anchor + value['interval_minutes'] * 60
            if self.last_attempt is not None:
                due = max(due, self.last_attempt + value['cooldown_minutes'] * 60)
            self.state['next_interval_at'] = (self.wall() + max(0, due - now)) if value['auto_enabled'] and value['interval_enabled'] else None
            pending_id = self.job_id
        if pending_id and hasattr(self.app.jobs, 'get'):
            record = self.app.jobs.get(pending_id)
            if record['status'] in ('completed', 'failed', 'cancelled'):
                self.finished(record.get('error') or ('自动清理已取消' if record['status'] == 'cancelled' else None))
        if not value['auto_enabled']:
            with self.lock:
                self.state['paused_reason'] = None
            return None
        if self.busy() or _clean_lock.locked():
            return None
        if getattr(self.app, 'maintenance', False):
            with self.lock:
                self.state['paused_reason'] = '数据维护期间暂停自动清理'
            return None
        percent = self.reader()['percent'] if value['threshold_enabled'] else None
        with self.lock:
            if percent is not None and percent <= value['threshold_percent'] - 5:
                self.armed = True
            threshold_due = percent is not None and percent >= value['threshold_percent'] and self.armed
            interval_due = value['interval_enabled'] and now - self.interval_anchor >= value['interval_minutes'] * 60
            if self.last_attempt is not None and now - self.last_attempt < value['cooldown_minutes'] * 60:
                self.state['paused_reason'] = '自动清理冷却中'
                return None
            if not threshold_due and not interval_due:
                self.state['paused_reason'] = None
                return None
        # This check never installs anything. clean(..., allow_install=False)
        # repeats the check atomically inside the broker before invoking a worker.
        try:
            helper = self.session.status()
            if not helper.get('installed'):
                with self.lock:
                    self.state['paused_reason'] = '请先启用或修复工具箱内存清理组件，自动清理不会请求管理员授权'
                return None
        except Exception as exc:
            with self.lock:
                self.state['paused_reason'] = '内存清理组件不可用：' + str(exc)
            return None
        with getattr(self.app, 'data_lock', contextlib.nullcontext()), self.lock:
            if self.stop.is_set() or getattr(self.app, 'maintenance', False) or self.settings.values() != value or self.busy() or _clean_lock.locked():
                return None
            trigger = 'threshold' if threshold_due else 'interval'
            self.last_attempt = now
            self.interval_anchor = now
            if threshold_due:
                self.armed = False
            self.state.update(running=True, last_trigger=trigger, paused_reason=None, last_error=None)
            try:
                record = self.app.jobs.submit('memory.clean', {'mode': value['auto_mode'], 'automatic': True, 'trigger': trigger})
                self.job_id = record['id']
                return record
            except Exception as exc:
                self.state.update(running=False, last_error=str(exc))
                raise

    def start(self):
        if self.thread is not None:
            return
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    with self.lock:
                        self.state['last_error'] = str(exc)
                self.changed.wait(15)
                self.changed.clear()
        self.thread = threading.Thread(target=loop, name='memory-auto-clean', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)


def register(app):
    from .memory_broker import PersistentMemoryCleaner
    session = PersistentMemoryCleaner()
    settings = MemorySettings(app)
    automation = MemoryAutomation(app, settings, session)
    def close():
        automation.close()
        session.close()
    app.memory_cleaner_close = close
    app.memory_cleaner_start = automation.start
    app.memory_automation = automation
    def run_clean(job):
        automation.started(job.params)
        try:
            result = clean(job, session)
        except Exception as exc:
            automation.finished(str(exc))
            raise
        automation.finished()
        return result
    def configure(job, remove=False):
        result = session.configure(job, remove=remove)
        automation.wake()
        return result
    app.register('memory.status', lambda _: snapshot())
    app.register('memory.helper.status', session.status)
    app.jobs.register('memory.helper.install', lambda job: configure(job))
    app.jobs.register('memory.helper.remove', lambda job: configure(job, remove=True))
    app.register('memory.helper.install', lambda _: app.jobs.submit('memory.helper.install', {}))
    app.register('memory.helper.remove', lambda _: app.jobs.submit('memory.helper.remove', {}))
    app.jobs.register('memory.clean', run_clean)
    app.register('memory.clean', settings.submit)
    app.register('memory.settings.get', settings.get)
    app.register('memory.settings.save', settings.save)
    app.register('memory.settings.open', lambda _: {'opened': True, 'page': 'memory'})
