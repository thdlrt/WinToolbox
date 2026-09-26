"""Physical memory telemetry and an explicitly invoked external cleaner."""
import ctypes
from ctypes import wintypes as w
import hashlib
import os
import threading
import json
_clean_lock = threading.Lock()
from pathlib import Path
from ..settings import atomic_json

MEMREDUCT_HASH = 'dd55a81d56e8c32918e5190b2ec2a1f2a7edbbca884627439b4b619fdfb5602d'


def snapshot():
    if os.name != 'nt': raise ValueError('内存悬浮球仅支持 Windows')
    class Memory(ctypes.Structure):
        _fields_ = [('length', w.DWORD), ('load', w.DWORD)] + [(key, ctypes.c_ulonglong) for key in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
    value = Memory(); value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)): raise ctypes.WinError()
    return {'percent': value.load, 'total': value.total, 'available': value.available, 'used': value.total - value.available,
            'commit_total': value.page_total, 'commit_used': value.page_total - value.page_available}


def executable():
    path = Path(os.environ.get('WINTOOLBOX_TOOLS', '')) / 'memreduct/memreduct.exe'
    if not path.is_file(): raise ValueError('缺少 Mem Reduct 组件，请更新完整便携版')
    if hashlib.sha256(path.read_bytes()).hexdigest() != MEMREDUCT_HASH: raise ValueError('Mem Reduct 校验失败，请重新安装组件')
    return path.resolve()


def clean_arguments(mode):
    if mode not in ('default', 'full'): raise ValueError('不支持的清理模式')
    return '-clean:full' if mode == 'full' else '-clean'


def _launch(path, mode='default', settings=False):
    arguments = '' if settings else clean_arguments(mode)
    class Execute(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('mask', w.ULONG), ('window', w.HWND), ('verb', w.LPCWSTR), ('file', w.LPCWSTR), ('parameters', w.LPCWSTR), ('directory', w.LPCWSTR), ('show', ctypes.c_int), ('instance', w.HINSTANCE), ('idlist', ctypes.c_void_p), ('class_name', w.LPCWSTR), ('class_key', w.HKEY), ('hotkey', w.DWORD), ('icon', w.HANDLE), ('process', w.HANDLE)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(Execute)]; shell.ShellExecuteExW.restype = w.BOOL
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]; kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    info = Execute(); info.size = ctypes.sizeof(info); info.mask = 0x40 | 0x100
    info.verb = 'runas'; info.file = str(path); info.parameters = arguments; info.directory = str(path.parent); info.show = 1
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == 1223: raise ValueError('已取消 Windows 管理员授权，未执行内存清理')
        raise ctypes.WinError(code)
    kernel.CloseHandle(info.process)


def clean(job, session):
    if not _clean_lock.acquire(blocking=False): raise ValueError('内存清理正在进行，请稍候')
    try:
        job.check_cancelled()
        before = snapshot()
        result = session.clean(job.params.get('mode', 'default'), job.progress)
        after = snapshot()
        return {**result, 'before': before, 'after': after,
                'available_change': after['available'] - before['available'],
                'message': '静默清理完成；可用内存变化以实时读数为准'}
    finally: _clean_lock.release()


class MemorySettings:
    def __init__(self, app):
        self.app = app
        self.path = app.data_dir / 'memory-cleaner.json'
        self.lock = threading.RLock()

    def get(self, _=None):
        with self.lock:
            value = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {}
            return {'mode': value.get('mode', 'default') if value.get('mode', 'default') in ('default', 'full') else 'default'}

    def save(self, params):
        mode = params.get('mode')
        clean_arguments(mode)
        with self.lock: atomic_json(self.path, {'mode': mode})
        return self.get()

    def submit(self, _):
        return self.app.jobs.submit('memory.clean', self.get())


def open_settings(job):
    # Explicit interactive action: the original UI owns its configuration and
    # resident auto-clean scheduler; never edit its cached INI behind its back.
    job.check_cancelled()
    path = executable()
    job.progress(10, '正在打开 Mem Reduct，等待 Windows 授权…')
    _launch(path, settings=True)
    return {'opened': True}


def register(app):
    from .memory_broker import PersistentMemoryCleaner
    session = PersistentMemoryCleaner()
    app.memory_cleaner_close = session.close
    settings = MemorySettings(app)
    app.register('memory.status', lambda _: snapshot())
    app.register('memory.helper.status', session.status)
    app.jobs.register('memory.helper.install', lambda job: session.configure(job))
    app.jobs.register('memory.helper.remove', lambda job: session.configure(job, remove=True))
    app.register('memory.helper.install', lambda _: app.jobs.submit('memory.helper.install', {}))
    app.register('memory.helper.remove', lambda _: app.jobs.submit('memory.helper.remove', {}))
    app.jobs.register('memory.clean', lambda job: clean(job, session))
    app.register('memory.clean', settings.submit)
    app.register('memory.settings.get', settings.get)
    app.register('memory.settings.save', settings.save)
    app.jobs.register('memory.configure', open_settings)
    app.register('memory.settings.open', lambda _: app.jobs.submit('memory.configure', {}))
