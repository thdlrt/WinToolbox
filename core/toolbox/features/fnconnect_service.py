"""Restricted local Windows service IPC. No passwords or executable/config paths."""
import json
import os
import subprocess
import time

PIPE = r'\\.\pipe\WinToolbox.Tun.v1'


def request(op, session=None, **params):
    if os.name != 'nt': raise OSError('Windows only')
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.WaitNamedPipeW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD]
    if not kernel.WaitNamedPipeW(PIPE,2000): raise OSError('TUN 辅助服务未安装或未启动')
    payload={'op':op,**params}
    if session: payload['session']=session
    # Server disconnects any stalled request after eight seconds.
    with open(PIPE,'r+b',buffering=0) as pipe:
        pipe.write((json.dumps(payload)+'\n').encode())
        result=bytearray()
        while len(result)<32768:
            char=pipe.read(1)
            if char==b'\n': break
            if not char: raise OSError('TUN 服务连接中断')
            result.extend(char)
    value=json.loads(result)
    if value.get('rpc_error'): raise RuntimeError(value['rpc_error'])
    return value


def install(tools):
    # Read the original user's SID before elevation (UAC may use another account).
    import csv,io
    result=subprocess.run(['whoami.exe','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    sid=next(csv.reader(io.StringIO(result.stdout)))[1]
    arguments=f'-NoProfile -ExecutionPolicy Bypass -File "{tools / "install-service.ps1"}" -OwnerSid "{sid}"'
    quoted="'"+arguments.replace("'","''")+"'"
    command=f'try {{ $p=Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -PassThru -Wait -ArgumentList {quoted}; exit $p.ExitCode }} catch {{ exit 1 }}'
    return subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
