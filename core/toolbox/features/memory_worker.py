"""Session-only elevated worker. Protocol accepts two fixed memory operations only.

Launched with pythonw -I: no toolbox imports, plugins, shell commands or paths
from the unprivileged client are evaluated. Closing the socket exits the worker.
Native enum reference: winsiderss/phnt, ntexapi.h SYSTEM_MEMORY_LIST_COMMAND.
"""
import ctypes
from ctypes import wintypes as w
import json
import socket
import sys


def commands(mode):
    if mode == 'default': return (2, 5)  # Working sets + low priority standby.
    if mode == 'full': return (2, 3, 4)  # Working sets + modified + all standby.
    raise ValueError('不支持的清理模式')


def enable_privilege():
    class Luid(ctypes.Structure):
        _fields_ = [('low', w.DWORD), ('high', w.LONG)]
    class Privilege(ctypes.Structure):
        _fields_ = [('count', w.DWORD), ('luid', Luid), ('attributes', w.DWORD)]
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(Luid)]
    adv.AdjustTokenPrivileges.argtypes = [w.HANDLE, w.BOOL, ctypes.POINTER(Privilege), w.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    token = w.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x28, ctypes.byref(token)): raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = Privilege(); value.count = 1; value.attributes = 2
        if not adv.LookupPrivilegeValueW(None, 'SeProfileSingleProcessPrivilege', ctypes.byref(value.luid)): raise ctypes.WinError(ctypes.get_last_error())
        ctypes.set_last_error(0)
        ok = adv.AdjustTokenPrivileges(token, False, ctypes.byref(value), 0, None, None)
        error = ctypes.get_last_error()
        if not ok or error: raise ctypes.WinError(error)
    finally: kernel.CloseHandle(token)


def clean_memory(mode):
    operations = commands(mode)
    enable_privilege()
    native = ctypes.WinDLL('ntdll')
    native.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, w.ULONG]
    native.NtSetSystemInformation.restype = w.LONG
    completed = []
    for operation in operations:
        value = w.ULONG(operation)
        status = native.NtSetSystemInformation(80, ctypes.byref(value), ctypes.sizeof(value))
        if status < 0:
            raise RuntimeError(f'内存区域 {operation} 清理失败（NTSTATUS 0x{status & 0xffffffff:08X}，已完成 {completed}）')
        completed.append(operation)
    return {'operations': completed}


def serve(connection, token, cleaner=clean_memory):
    with connection, connection.makefile('rwb', buffering=0) as stream:
        stream.write((json.dumps({'token': token}) + '\n').encode())
        while True:
            raw = stream.readline(1025)
            if not raw: return
            if len(raw) > 1024 or not raw.endswith(b'\n'): return
            try:
                request = json.loads(raw)
                if not isinstance(request, dict) or set(request) != {'mode'}: raise ValueError('无效清理请求')
                commands(request['mode'])
                result = {'ok': True, **cleaner(request['mode'])}
            except Exception as error:
                result = {'ok': False, 'error': str(error)}
            stream.write((json.dumps(result) + '\n').encode())


if __name__ == '__main__':
    if len(sys.argv) != 3 or len(sys.argv[2]) != 64: sys.exit(2)
    with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=15) as channel:
        channel.settimeout(None)
        serve(channel, sys.argv[2])
