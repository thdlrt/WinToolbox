"""Own one elevated cleaner per backend session; never elevate the toolbox UI."""
import ctypes
from ctypes import wintypes as w
import hmac
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time


def launch_worker(port, token):
    # Avoid a virtualenv redirector: elevation must start the actual interpreter.
    executable = Path(sys.base_prefix) / 'pythonw.exe'
    if not executable.is_file(): raise ValueError('缺少静默清理运行环境 pythonw.exe')
    script = Path(__file__).with_name('memory_worker.py').resolve()
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_int]
    shell.ShellExecuteW.restype = ctypes.c_void_p
    result = shell.ShellExecuteW(None, 'runas', str(executable), subprocess.list2cmdline(['-I', str(script), str(port), token]), str(executable.parent), 0)
    if not result or result <= 32:
        raise ValueError(f'未获得 Windows 管理员授权，未执行内存清理（代码 {result or ctypes.get_last_error()}）')


class MemorySession:
    def __init__(self, launcher=launch_worker):
        self.launcher = launcher
        self.connection = None
        self.stream = None
        self.lock = threading.Lock()

    def close(self):
        # shutdown wakes the worker even while the stream retains the socket.
        if self.connection:
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        if self.stream: self.stream.close()
        if self.connection: self.connection.close()
        self.stream = self.connection = None

    def connect(self):
        token = secrets.token_hex(32)
        with socket.socket() as listener:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', 0)); listener.listen(4)
            self.launcher(listener.getsockname()[1], token)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                listener.settimeout(max(.1, deadline - time.monotonic()))
                connection, _ = listener.accept()
                connection.settimeout(2)
                stream = connection.makefile('rwb', buffering=0)
                try:
                    auth = json.loads(stream.readline(1025))
                    if isinstance(auth.get('token'), str) and hmac.compare_digest(auth['token'], token):
                        connection.settimeout(90)
                        self.connection, self.stream = connection, stream
                        return
                except (ValueError, OSError, AttributeError): pass
                stream.close(); connection.close()
            raise ValueError('静默清理进程未连接，请重试')

    def clean(self, mode, progress):
        from .memory_worker import commands
        commands(mode)
        if not self.lock.acquire(blocking=False): raise ValueError('内存清理正在进行，请稍候')
        try:
            if not self.connection:
                progress(5, '首次清理需要 Windows 授权；本次工具箱运行期间仅需一次')
                self.connect()
            progress(25, '正在静默清理内存…')
            self.stream.write((json.dumps({'mode': mode}) + '\n').encode())
            raw = self.stream.readline(4097)
            if not raw or len(raw) > 4096: raise ValueError('清理进程连接中断，结果未确认；请重试')
            result = json.loads(raw)
            if not result.get('ok'): raise ValueError(result.get('error', '内存清理失败'))
            if result.get('operations') != list(commands(mode)): raise ValueError('清理结果不完整')
            return result
        except (OSError, ValueError):
            self.close()  # No automatic retry: cleanup must never run twice.
            raise
        finally: self.lock.release()
