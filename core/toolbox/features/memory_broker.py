"""Persistent permission, short-lived fixed-purpose worker; no elevated UI."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading

from .memory_worker import commands


def executable():
    directory = Path(os.environ.get('WINTOOLBOX_TOOLS', '')) / 'memory-cleaner'
    path = directory / 'WinToolbox.MemoryCleaner.exe'
    manifest = directory / 'manifest.json'
    if not path.is_file() or not manifest.is_file():
        raise ValueError('缺少免重复授权清理组件，请更新完整便携版')
    expected = json.loads(manifest.read_text('utf-8-sig')).get('sha256')
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('内存清理组件校验失败，请更新完整便携版')
    return path.resolve()


def invoke(path, *arguments):
    result = subprocess.run([str(path), *arguments], capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=115, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('无法读取内存清理组件的响应，请在内存设置中修复组件') from None
    if not isinstance(value, dict) or not value.get('ok'):
        raise ValueError(value.get('error', '内存清理未完成') if isinstance(value, dict) else '清理响应无效')
    if result.returncode != 0:
        raise ValueError('内存清理组件未正常退出')
    return value


def elevate(path, action, sid):
    if action not in ('--install', '--uninstall') or not re.fullmatch(r'S-1-(?:5-21|12-1)-(?:[0-9]+-){3}[0-9]+', sid):
        raise ValueError('无效清理组件操作')
    class Execute(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('mask', w.ULONG), ('window', w.HWND), ('verb', w.LPCWSTR), ('file', w.LPCWSTR),
                    ('parameters', w.LPCWSTR), ('directory', w.LPCWSTR), ('show', ctypes.c_int), ('instance', w.HINSTANCE),
                    ('idlist', ctypes.c_void_p), ('class_name', w.LPCWSTR), ('class_key', w.HKEY), ('hotkey', w.DWORD),
                    ('icon', w.HANDLE), ('process', w.HANDLE)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(Execute)]; shell.ShellExecuteExW.restype = w.BOOL
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]; kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    info = Execute(); info.size = ctypes.sizeof(info); info.mask = 0x40 | 0x100
    info.verb = 'runas'; info.file = str(path); info.parameters = subprocess.list2cmdline([action, sid])
    info.directory = str(path.parent); info.show = 0
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == 1223: raise ValueError('已取消组件安装授权，未执行清理；下次可重新启用')
        raise ctypes.WinError(code)
    try:
        if kernel.WaitForSingleObject(info.process, 120000) != 0:
            raise ValueError('组件安装尚未确认完成，请稍后重新操作')
        code = w.DWORD()
        if not kernel.GetExitCodeProcess(info.process, ctypes.byref(code)): raise ctypes.WinError(ctypes.get_last_error())
        if code.value: raise ValueError(f'清理组件安装或移除失败（0x{code.value:08X}）')
    finally: kernel.CloseHandle(info.process)


class PersistentMemoryCleaner:
    def __init__(self, runner=invoke, installer=elevate, path_provider=executable):
        self.runner, self.installer, self.path_provider = runner, installer, path_provider
        self.lock = threading.Lock()

    def identity(self):
        path = self.path_provider()
        sid = self.runner(path, '--identity').get('sid', '')
        if not isinstance(sid, str) or not re.fullmatch(r'S-1-(?:5-21|12-1)-(?:[0-9]+-){3}[0-9]+', sid):
            raise ValueError('无法识别当前 Windows 账户')
        return path, sid

    def status(self, _=None):
        path, sid = self.identity()
        value = self.runner(path, '--status', sid)
        return {'installed': bool(value.get('installed')), 'present': bool(value.get('present')), 'protocol': value.get('protocol')}

    def _install(self, path, sid, progress):
        progress(5, '首次安装免重复授权组件，需要一次 Windows 授权')
        self.installer(path, '--install', sid)
        if not self.runner(path, '--status', sid).get('installed'):
            raise ValueError('清理组件未成功注册，请在内存设置中重试启用')

    def configure(self, job, remove=False):
        if not self.lock.acquire(blocking=False): raise ValueError('内存清理正在进行，请稍候')
        try:
            job.check_cancelled(); path, sid = self.identity()
            if remove:
                job.progress(10, '正在移除免重复授权组件，需要 Windows 授权')
                self.installer(path, '--uninstall', sid)
            else:
                self._install(path, sid, job.progress)
            return self.status()
        finally: self.lock.release()

    def clean(self, mode, progress):
        expected = list(commands(mode))
        if not self.lock.acquire(blocking=False): raise ValueError('内存清理正在进行，请稍候')
        try:
            path, sid = self.identity()
            status = self.runner(path, '--status', sid)
            if not status.get('installed'):
                if status.get('present'):
                    raise ValueError('清理组件已安装但不可用，请在内存清理设置中点击修复；不会自动重复申请权限')
                self._install(path, sid, progress)
            progress(25, '正在静默清理内存…')
            # Failures never trigger reinstallation, elevation or automatic cleanup retries.
            result = self.runner(path, '--request', sid, mode)
            if result.get('protocol') != 1 or result.get('operations') != expected:
                raise ValueError('内存清理结果不完整，请检查组件版本')
            return result
        finally: self.lock.release()

    def close(self):
        # Task registration survives restarts. The on-demand broker exits when idle.
        pass
