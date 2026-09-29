"""Persistent permission, short-lived fixed-purpose worker; no elevated UI."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import logging
import ntpath
import os
from pathlib import Path
import re
import subprocess
import threading

from .memory_worker import commands

log = logging.getLogger(__name__)

INSTALL_STEPS = {
    1: '检查管理员权限',
    2: '准备受保护的组件目录',
    3: '连接 Windows 计划任务服务',
    4: '复制清理组件',
    5: '设置组件文件权限',
    6: '创建计划任务配置',
    7: '注册 Windows 计划任务',
}


def install_failure(code):
    if code & 0xff000000 == 0x60000000:
        step = INSTALL_STEPS.get((code >> 16) & 0xff)
        if step:
            detail = f'{step}失败（Windows 错误 0x{code & 0xffff:04X}）'
            if (code & 0xffff) == 2:
                detail += '：找不到所需文件或计划任务；请检查组件文件和 Windows 计划任务服务'
            return detail
    if code == 0x80070002:
        return '清理组件安装失败（0x80070002：找不到文件或计划任务）；请更新完整便携版后重试'
    return f'清理组件安装或移除失败（0x{code:08X}）'


def launch_path(path):
    """.NET Framework cannot initialize from a Win32 extended-length path."""
    value = os.fspath(path)
    if value.lower().startswith('\\\\?\\unc\\'):
        return '\\\\' + value[8:]
    if value.startswith('\\\\?\\'):
        value = value[4:]
        if not re.match(r'^[A-Za-z]:\\', value):
            raise ValueError('内存清理组件需要普通 Windows 驱动器或 UNC 路径')
    return value


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
    command = [launch_path(path), *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=115, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        log.exception('Memory cleaner launch failed: action=%s path=%s', arguments[:1], command[0])
        raise ValueError(f'内存清理组件启动失败：{error}；详情见后端日志') from error
    def failure():
        log.error('Memory cleaner response failure: action=%s exit=%s stdout=%r stderr=%r',
                  arguments[:1], result.returncode, result.stdout[:4000], result.stderr[:4000])
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        failure()
        raise ValueError(f'内存清理组件未返回有效响应（退出码 {result.returncode}），详情见后端日志') from None
    if not isinstance(value, dict) or not value.get('ok'):
        failure()
        raise ValueError(value.get('error', '内存清理未完成') if isinstance(value, dict) else '清理响应无效')
    if result.returncode != 0:
        failure()
        raise ValueError(f'内存清理组件未正常退出（退出码 {result.returncode}），详情见后端日志')
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
    info.verb = 'runas'; info.file = launch_path(path); info.parameters = subprocess.list2cmdline([action, sid])
    info.directory = ntpath.dirname(info.file); info.show = 0
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == 1223: raise ValueError('已取消组件安装授权，未执行清理；下次可重新启用')
        raise ctypes.WinError(code)
    try:
        if kernel.WaitForSingleObject(info.process, 120000) != 0:
            raise ValueError('组件安装尚未确认完成，请稍后重新操作')
        code = w.DWORD()
        if not kernel.GetExitCodeProcess(info.process, ctypes.byref(code)): raise ctypes.WinError(ctypes.get_last_error())
        if code.value: raise ValueError(install_failure(code.value))
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

    def clean(self, mode, progress, allow_install=True):
        expected = list(commands(mode))
        if not self.lock.acquire(blocking=False): raise ValueError('内存清理正在进行，请稍候')
        try:
            path, sid = self.identity()
            status = self.runner(path, '--status', sid)
            if not status.get('installed'):
                if not allow_install:
                    raise ValueError('自动清理已跳过：请先在内存清理设置中启用清理组件')
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
