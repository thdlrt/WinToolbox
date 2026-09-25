"""Read OS GPU counters without NVML, nvidia-smi, or creating a D3D device."""
import ctypes as C
from ctypes import wintypes as W
from functools import lru_cache
import os
import re
import shutil
import subprocess
import time


def require_windows():
    if os.name != 'nt':
        raise RuntimeError('独显守卫仅支持 Windows 10/11')


def power_status():
    require_windows()
    class Power(C.Structure):
        _fields_ = [('ac', W.BYTE), ('flags', W.BYTE), ('percent', W.BYTE),
                    ('reserved', W.BYTE), ('life', W.DWORD), ('full', W.DWORD)]
    value = Power()
    if not C.windll.kernel32.GetSystemPowerStatus(C.byref(value)):
        raise C.WinError()
    battery = value.flags != 255 and not bool(value.flags & 128)
    result = {'source': 'battery' if battery and value.ac == 0 else
            'ac' if value.ac == 1 else 'unknown', 'has_battery': battery,
            'percent': value.percent if value.percent <= 100 else None}
    class Battery(C.Structure):
        _fields_ = [('ac', W.BYTE), ('present', W.BYTE), ('charging', W.BYTE),
                    ('discharging', W.BYTE), ('spare', W.BYTE * 3), ('tag', W.BYTE),
                    ('capacity', W.DWORD), ('remaining', W.DWORD), ('rate', W.DWORD),
                    ('seconds', W.DWORD), ('alert1', W.DWORD), ('alert2', W.DWORD)]
    detail = Battery()
    result.update(discharge_w=None, remaining_wh=None, remaining_seconds=None, cpu_w=None, gpu_w=None)
    status = C.windll.powrprof.CallNtPowerInformation(5, None, 0, C.byref(detail), C.sizeof(detail))
    if status == 0 and detail.present:
        result.update(battery_metrics(detail.rate, detail.remaining, detail.seconds, bool(detail.discharging)))
    if result['remaining_seconds'] is None and result['source'] == 'battery' and value.life not in (0, 0xffffffff):
        result['remaining_seconds'] = value.life
    result['runtime_source'] = 'system'
    if result['remaining_seconds'] is None and result['discharge_w'] and result['remaining_wh']:
        result['remaining_seconds'] = int(result['remaining_wh'] * 3600 / result['discharge_w'])
        result['runtime_source'] = 'current_load'
    return result


def battery_metrics(rate, remaining, seconds, discharging):
    # Rate is DWORD in the ABI but signed LONG in the documented interpretation.
    signed = rate if rate < 0x80000000 else rate - 0x100000000
    watts = abs(signed) / 1000 if discharging and rate not in (0, 0xffffffff, 0x80000000) else None
    return {'discharge_w': watts, 'remaining_wh': remaining / 1000 if remaining not in (0xffffffff, 0) else None,
            'remaining_seconds': seconds if discharging and seconds not in (0xffffffff, 0) else None}


def gpu_power_once():
    require_windows()
    exe = shutil.which('nvidia-smi.exe')
    if not exe:
        raise RuntimeError('未找到 nvidia-smi，无法手动读取独显功率')
    completed = subprocess.run([exe, '--query-gpu=name,power.draw', '--format=csv,noheader,nounits'],
                               capture_output=True, text=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    readings = []
    for line in completed.stdout.splitlines():
        name, _, value = line.rpartition(',')
        try:
            watts = float(value.strip())
        except ValueError:
            watts = None
        readings.append({'name': name.strip(), 'watts': watts})
    return {'readings': readings, 'sampled_at': time.time()}


@lru_cache(maxsize=1)
def adapters():
    # Enumerate topology once per backend lifetime; never recreate DXGI on each poll.
    require_windows()
    class Guid(C.Structure):
        _fields_ = [('a', W.DWORD), ('b', W.WORD), ('c', W.WORD), ('d', W.BYTE * 8)]
    class Luid(C.Structure):
        _fields_ = [('low', W.DWORD), ('high', W.LONG)]
    class Desc(C.Structure):
        _fields_ = [('name', W.WCHAR * 128), ('vendor', W.UINT), ('device', W.UINT),
                    ('subsystem', W.UINT), ('revision', W.UINT), ('dedicated', C.c_size_t),
                    ('system', C.c_size_t), ('shared', C.c_size_t), ('luid', Luid), ('flags', W.UINT)]
    def method(ptr, slot, result, *args):
        table = C.cast(ptr, C.POINTER(C.POINTER(C.c_void_p))).contents
        return C.WINFUNCTYPE(result, C.c_void_p, *args)(table[slot])
    iid = Guid(0x770aae78, 0xf26f, 0x4dba, (W.BYTE * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
    factory = C.c_void_p()
    create = C.windll.dxgi.CreateDXGIFactory1
    create.argtypes = [C.POINTER(Guid), C.POINTER(C.c_void_p)]
    create.restype = W.LONG
    if create(C.byref(iid), C.byref(factory)) < 0:
        raise RuntimeError('无法枚举显卡，请检查显示驱动')
    result = []
    try:
        enum = method(factory, 12, W.LONG, W.UINT, C.POINTER(C.c_void_p))
        for index in range(32):
            adapter = C.c_void_p()
            hr = enum(factory, index, C.byref(adapter))
            if hr & 0xffffffff == 0x887a0002:
                break
            if hr < 0:
                raise RuntimeError('显卡枚举失败')
            try:
                desc = Desc()
                if method(adapter, 10, W.LONG, C.POINTER(Desc))(adapter, C.byref(desc)) < 0:
                    raise RuntimeError('无法读取显卡标识')
                if not desc.flags & 2:
                    result.append({'name': desc.name, 'vendor': desc.vendor,
                                   'luid': f'0x{desc.luid.high & 0xffffffff:08x}_0x{desc.luid.low:08x}'})
            finally:
                method(adapter, 2, W.ULONG)(adapter)
    finally:
        method(factory, 2, W.ULONG)(factory)
    return result


def process_path(pid):
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ''
    try:
        buffer = C.create_unicode_buffer(32768)
        size = W.DWORD(len(buffer))
        return buffer.value if kernel.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(size)) else ''
    finally:
        kernel.CloseHandle(handle)


def counter_sample():
    require_windows()
    class ValueUnion(C.Union):
        _fields_ = [('double', C.c_double), ('large', C.c_longlong), ('long', W.LONG), ('text', W.LPWSTR)]
    class Value(C.Structure):
        _anonymous_ = ('value',)
        _fields_ = [('status', W.DWORD), ('value', ValueUnion)]
    class Item(C.Structure):
        _fields_ = [('name', W.LPWSTR), ('value', Value)]
    pdh = C.WinDLL('pdh')
    pdh.PdhOpenQueryW.argtypes = [W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
    pdh.PdhAddEnglishCounterW.argtypes = [W.HANDLE, W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
    pdh.PdhCollectQueryData.argtypes = [W.HANDLE]
    pdh.PdhCloseQuery.argtypes = [W.HANDLE]
    pdh.PdhGetFormattedCounterArrayW.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.c_void_p]
    def check(code):
        if code:
            raise RuntimeError(f'Windows GPU 性能计数器不可用（0x{code & 0xffffffff:08x}），请检查显卡驱动')
    query = W.HANDLE()
    check(pdh.PdhOpenQueryW(None, 0, C.byref(query)))
    try:
        handles = {}
        for name, path in [('usage', r'\GPU Engine(*)\Utilization Percentage'),
                           ('memory', r'\GPU Process Memory(*)\Dedicated Usage')]:
            handle = W.HANDLE()
            check(pdh.PdhAddEnglishCounterW(query, path, 0, C.byref(handle)))
            handles[name] = handle
        check(pdh.PdhCollectQueryData(query))
        time.sleep(1)
        check(pdh.PdhCollectQueryData(query))
        result = {}
        for name, handle in handles.items():
            size, count = W.DWORD(), W.DWORD()
            code = pdh.PdhGetFormattedCounterArrayW(handle, 0x200, C.byref(size), C.byref(count), None)
            if code & 0xffffffff == 0x800007d5:  # PDH_NO_DATA: never interpret as powered off.
                result[name] = []
                continue
            if code & 0xffffffff != 0x800007d2:
                check(code)
            if not size.value:
                result[name] = []
                continue
            buffer = C.create_string_buffer(size.value)
            check(pdh.PdhGetFormattedCounterArrayW(handle, 0x200, C.byref(size), C.byref(count), buffer))
            items = C.cast(buffer, C.POINTER(Item))
            result[name] = [(items[i].name, items[i].value.double) for i in range(count.value)
                            if items[i].value.status in (0, 1)]
        return result
    finally:
        pdh.PdhCloseQuery(query)


def merge_counters(gpus, counters, resolve=process_path):
    targets = {gpu['luid'].lower(): gpu['name'] for gpu in gpus if gpu['vendor'] == 0x10de}
    rows = {}
    for kind, samples in counters.items():
        for name, value in samples:
            match = re.search(r'pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)', name, re.I)
            if not match or match[2].lower() not in targets or value <= 0:
                continue
            pid, luid = int(match[1]), match[2].lower()
            key = (pid, luid)
            row = rows.setdefault(key, {'pid': pid, 'gpu': targets[luid], 'luid': luid, 'usage': 0., 'memory_mb': 0.})
            # Engines can work in parallel; report the busiest, not an invalid sum.
            if kind == 'usage':
                row['usage'] = max(row['usage'], min(100., value))
            else:
                row['memory_mb'] += value / 1048576
    for row in rows.values():
        row['path'] = resolve(row['pid'])
        row['name'] = row['path'].replace('\\', '/').rsplit('/', 1)[-1] or f"PID {row['pid']}（路径不可读）"
        row['usage'] = round(row['usage'], 2)
        row['memory_mb'] = round(row['memory_mb'], 1)
    return sorted(rows.values(), key=lambda row: (-row['usage'], -row['memory_mb']))


def snapshot():
    gpus = adapters()
    rows = merge_counters(gpus, counter_sample()) if any(g['vendor'] == 0x10de for g in gpus) else []
    return {'adapters': gpus, 'processes': rows, 'sampled_at': time.time(),
            'power_state': 'unknown', 'power': power_status()}
