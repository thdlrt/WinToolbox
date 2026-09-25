"""Session-scoped, reversible per-application battery GPU preferences."""
import atexit
import json
import os
from pathlib import Path
import re
import threading
import time

from ..settings import atomic_json
from . import gpu_native


class Preferences:
    KEY = r'Software\Microsoft\DirectX\UserGpuPreferences'

    def read(self, path):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
                value, kind = winreg.QueryValueEx(key, path)
            if kind != winreg.REG_SZ:
                raise RuntimeError('已有显卡偏好格式不支持，已保留原设置')
            return value
        except FileNotFoundError:
            return None

    def write(self, path, value):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_SET_VALUE) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, path)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, path, 0, winreg.REG_SZ, value)


def prefer_integrated(value):
    parts = [part for part in (value or '').split(';') if part and not re.match(r'^\s*GpuPreference\s*=', part, re.I)]
    return ';'.join(parts + ['GpuPreference=1']) + ';'


class Guard:
    def __init__(self, root, *, preferences=None, native=None, emit=None, exclusive=True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.prefs = preferences or Preferences()
        self.native = native or gpu_native
        self.emit = emit or (lambda **event: None)
        self.enabled = False
        self.active = False
        self.error = ''
        self.notice = ''
        self.sample = None
        self.power = {'source': 'unknown', 'has_battery': False, 'percent': None}
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.last_scan = 0.
        self.last_alert = 0.
        self.closed = False
        self.lease = None
        if exclusive:
            import msvcrt
            self.lease = open(self.root / 'owner.lock', 'a+b')
            self.lease.seek(0)
            if not self.lease.read(1):
                self.lease.write(b'0'); self.lease.flush()
            self.lease.seek(0)
            try:
                msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.lease.close(); self.lease = None
                raise RuntimeError('另一个工具箱实例正在使用独显守卫，请先退出该实例')
        try:
            self.apps = self._load('apps.json', {'apps': []})['apps']
            self.journal = self._load('recovery.json', {'entries': {}})['entries']
            if not isinstance(self.apps, list) or not all(isinstance(p, str) for p in self.apps) or not isinstance(self.journal, dict):
                raise ValueError('守卫配置格式无效')
            self.restore()
        except Exception:
            if self.lease:
                self.lease.close()
            raise

    def _load(self, name, fallback):
        path = self.root / name
        return json.loads(path.read_text('utf-8')) if path.exists() else fallback

    def _save_journal(self):
        atomic_json(self.root / 'recovery.json', {'entries': self.journal})

    def restore(self):
        with self.lock:
            conflicts, failures = [], []
            for path, entry in list(self.journal.items()):
                try:
                    current = self.prefs.read(path)
                    if current == entry['applied']:
                        self.prefs.write(path, entry['original'])
                    elif current != entry['original']:
                        conflicts.append(Path(path).name)
                    del self.journal[path]
                    self._save_journal()
                except Exception as exc:
                    # Keep recovery entry when registry or disk IO failed.
                    self.journal[path] = entry
                    failures.append(str(exc))
            self.active = bool(self.journal)
            if conflicts:
                self.notice = '以下应用的设置已被其他程序修改，保留其新值：' + '、'.join(conflicts)
            if failures:
                raise RuntimeError('部分设置未恢复，请重试停用或重新打开本页：' + failures[0])

    def apply(self):
        with self.lock:
            if self.active:
                return
            if self.journal:
                self.restore()
            try:
                for path in self.apps:
                    if not Path(path).is_file():
                        raise RuntimeError('应用文件已不存在，请停用并移除：' + path)
                    original = self.prefs.read(path)
                    applied = prefer_integrated(original)
                    if original == applied:
                        continue
                    # Durable write-ahead recovery, before changing the real preference.
                    self.journal[path] = {'original': original, 'applied': applied}
                    self._save_journal()
                    self.prefs.write(path, applied)
                self.active = True
            except Exception:
                self.restore()
                raise

    def edit(self, params):
        with self.lock:
            if self.enabled or self.journal:
                raise RuntimeError('请先停用并恢复设置，再编辑应用名单')
            raw = str(params.get('path', '')).strip().strip('"')
            if params.get('remove'):
                apps = [p for p in self.apps if p.casefold() != raw.casefold()]
            else:
                path = Path(raw)
                if not path.is_absolute() or path.suffix.lower() != '.exe' or not path.is_file():
                    raise ValueError('请选择本机存在的 .exe 应用程序')
                resolved = str(path.resolve())
                # Do not target protected OS rendering processes through this UI.
                system = Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve()
                if path.resolve().is_relative_to(system):
                    raise ValueError('请选择普通应用，系统组件不加入省电名单')
                apps = list(self.apps)
                if resolved.casefold() not in {p.casefold() for p in apps}:
                    if len(apps) >= 100:
                        raise ValueError('最多添加 100 个应用')
                    apps.append(resolved)
            atomic_json(self.root / 'apps.json', {'apps': apps})
            self.apps = apps
            return self.status()

    def status(self):
        with self.lock:
            return {'enabled': self.enabled, 'active': self.active, 'apps': list(self.apps),
                    'pending_restore': len(self.journal), 'power': dict(self.power),
                    'sample': self.sample, 'error': self.error, 'notice': self.notice,
                    'supported': os.name == 'nt',
                    'software_rendering': (self.root / 'software-rendering.flag').exists()}

    def rendering(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled 必须为布尔值')
        with self.lock:
            flag = self.root / 'software-rendering.flag'
            if enabled:
                flag.write_text('1', encoding='utf-8')
            else:
                flag.unlink(missing_ok=True)
            return self.status()

    def refresh_power(self):
        with self.lock:
            self.power = self.native.power_status()
            return self.status()

    def set_enabled(self, value):
        if not isinstance(value, bool):
            raise ValueError('enabled 必须为布尔值')
        with self.lock:
            if self.closed:
                raise RuntimeError('守卫已退出，请重新启动工具箱')
            self.error = ''
            if not value:
                self.enabled = False
                self.restore()
            else:
                self.power = self.native.power_status()
                if not self.power['has_battery']:
                    raise RuntimeError('未检测到电池；台式机可以检查占用，但不能启用电池守卫')
                if not self.apps:
                    raise ValueError('请先添加需要优先使用核显的应用')
                if not any(g['vendor'] == 0x10de for g in self.native.adapters()):
                    raise RuntimeError('未检测到 NVIDIA 显卡')
                self.enabled = True
                try:
                    self.tick()
                except Exception:
                    self.enabled = False
                    raise
                if not self.thread:
                    self.thread = threading.Thread(target=self._loop, name='gpu-battery-guard', daemon=True)
                    self.thread.start()
            self.wake_event.set()
            return self.status()

    def tick(self):
        with self.lock:
            self.power = self.native.power_status()
            if self.enabled and self.power['source'] == 'battery':
                self.apply()
            elif self.active or self.journal:
                self.restore()

    def scan(self):
        with self.scan_lock:
            with self.lock:
                self.last_scan = time.monotonic()
            try:
                sample = self.native.snapshot()
                with self.lock:
                    self.sample = sample
                    self.power = sample['power']
                    self.error = ''
                    self.last_scan = time.monotonic()
                    if self.enabled and self.power['source'] == 'battery' and any(p['usage'] > 0 for p in sample['processes']):
                        now = time.monotonic()
                        if now - self.last_alert > 300:
                            self.last_alert = now
                            self.emit(message='电池模式检测到独显活动，请在“独显省电守卫”查看占用应用。')
                return self.status()
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
                raise

    def _loop(self):
        while not self.stop_event.is_set():
            self.wake_event.wait(5 if self.enabled else None)
            self.wake_event.clear()
            if self.stop_event.is_set():
                break
            try:
                with self.lock:
                    if not self.enabled:
                        continue
                    self.tick()
                    should_scan = self.power['source'] == 'battery' and time.monotonic() - self.last_scan >= 30
                if should_scan:
                    self.scan()
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)
        with self.lock:
            if self.closed:
                return
            self.enabled = False
            try:
                self.restore()
            finally:
                self.closed = True
                if self.lease:
                    self.lease.close()


def register(app):
    holder = {}
    lock = threading.Lock()
    def guard():
        gpu_native.require_windows()
        with lock:
            if 'guard' not in holder:
                root = Path(os.environ['LOCALAPPDATA']) / 'WinToolbox' / 'gpu-guard'
                holder['guard'] = Guard(root, emit=lambda **event: app.emit('gpu_guard.activity', **event))
            return holder['guard']
    def close():
        if holder.get('guard'):
            holder['guard'].close()
    app.gpu_guard_close = close
    atexit.register(close)
    app.register('gpu_guard.status', lambda p: guard().refresh_power())
    app.register('gpu_guard.apps', lambda p: guard().edit(p))
    app.register('gpu_guard.enable', lambda p: guard().set_enabled(p.get('enabled')))
    app.register('gpu_guard.rendering', lambda p: guard().rendering(p.get('enabled')))
    app.jobs.register('gpu_guard.scan', lambda job: guard().scan())
    app.register('gpu_guard.scan', lambda p: app.jobs.submit('gpu_guard.scan', {}))
    app.jobs.register('gpu_guard.power', lambda job: gpu_native.gpu_power_once())
    app.register('gpu_guard.power', lambda p: app.jobs.submit('gpu_guard.power', {}))
