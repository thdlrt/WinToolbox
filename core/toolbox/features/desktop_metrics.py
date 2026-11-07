"""Low-impact Windows telemetry for the desk display; no vendor process or D3D device."""
import ctypes as C
from ctypes import wintypes as W
import re
import time

from . import gpu_native, system_memory


class Metrics:
    def __init__(self):
        self.previous = None
        self.query = None
        self.handles = {}
        self.gpus = None
        self.gpu_error = None

    def cpu(self):
        idle, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME()
        if not C.windll.kernel32.GetSystemTimes(C.byref(idle), C.byref(kernel), C.byref(user)):
            raise C.WinError()
        values = tuple(v.dwLowDateTime + (v.dwHighDateTime << 32) for v in (idle, kernel, user))
        old, self.previous = self.previous, values
        if old is None:
            return None
        di, dk, du = (v - p for v, p in zip(values, old))
        total = dk + du
        return round(max(0, min(100, 100 * (total - di) / total)), 1) if total > 0 else None

    def _open_gpu(self):
        self.gpus = gpu_native.adapters()
        pdh = C.WinDLL('pdh')
        pdh.PdhOpenQueryW.argtypes = [W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
        pdh.PdhAddEnglishCounterW.argtypes = [W.HANDLE, W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
        pdh.PdhCollectQueryData.argtypes = [W.HANDLE]
        pdh.PdhCloseQuery.argtypes = [W.HANDLE]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.c_void_p]
        query = W.HANDLE()
        if pdh.PdhOpenQueryW(None, 0, C.byref(query)):
            raise RuntimeError('无法打开 GPU 性能计数器')
        self.pdh, self.query = pdh, query
        for name, path in [('usage', r'\GPU Engine(*)\Utilization Percentage'),
                           ('memory', r'\GPU Adapter Memory(*)\Dedicated Usage')]:
            handle = W.HANDLE()
            if pdh.PdhAddEnglishCounterW(query, path, 0, C.byref(handle)) == 0:
                self.handles[name] = handle
        pdh.PdhCollectQueryData(query)

    def _array(self, handle):
        class Union(C.Union):
            _fields_ = [('double', C.c_double), ('large', C.c_longlong), ('long', W.LONG), ('text', W.LPWSTR)]
        class Value(C.Structure):
            _anonymous_ = ('value',)
            _fields_ = [('status', W.DWORD), ('value', Union)]
        class Item(C.Structure):
            _fields_ = [('name', W.LPWSTR), ('value', Value)]
        size, count = W.DWORD(), W.DWORD()
        code = self.pdh.PdhGetFormattedCounterArrayW(handle, 0x200, C.byref(size), C.byref(count), None) & 0xffffffff
        if code == 0x800007d5:
            return None
        if code != 0x800007d2 or not size.value:
            return None
        buffer = C.create_string_buffer(size.value)
        code = self.pdh.PdhGetFormattedCounterArrayW(handle, 0x200, C.byref(size), C.byref(count), buffer)
        if code:
            return None
        items = C.cast(buffer, C.POINTER(Item))
        return [(items[i].name, items[i].value.double) for i in range(count.value) if items[i].value.status in (0, 1)]

    def gpu(self, selected=''):
        if self.query is None:
            self._open_gpu()
            return {'available': False, 'error': '正在采样'}
        if self.pdh.PdhCollectQueryData(self.query):
            raise RuntimeError('GPU 性能计数器采样失败')
        gpu = next((g for g in self.gpus if g['luid'] == selected), None)
        gpu = gpu or next((g for g in self.gpus if g.get('dedicated_bytes', 0) > 1024 ** 3), None)
        gpu = gpu or next(iter(self.gpus), None)
        if not gpu:
            return {'available': False, 'error': '未找到显卡'}
        luid = gpu['luid'].lower()
        engines, memory = {}, None
        usage = self._array(self.handles['usage']) if 'usage' in self.handles else None
        for name, value in usage or []:
            if luid not in name.lower():
                continue
            match = re.search(r'_eng_(\d+)_', name)
            if match:
                engines[match[1]] = engines.get(match[1], 0) + max(0, value)
        samples = self._array(self.handles['memory']) if 'memory' in self.handles else None
        matched = [v for name, v in samples or [] if luid in name.lower()]
        if matched:
            memory = max(matched)
        return {'available': usage is not None, 'name': gpu['name'], 'luid': luid,
                'percent': round(min(100, max(engines.values(), default=0)), 1) if usage is not None else None,
                'used_bytes': memory, 'total_bytes': gpu.get('dedicated_bytes') or None,
                'adapters': [{'name': g['name'], 'luid': g['luid']} for g in self.gpus]}

    def sample(self, gpu=''):
        result = {'sampled_at': time.time()}
        for name, reader in [('cpu', lambda: {'percent': self.cpu()}), ('ram', system_memory.snapshot), ('gpu', lambda: self.gpu(gpu))]:
            try:
                result[name] = reader()
            except Exception as exc:
                result[name] = {'available': False, 'error': str(exc)}
        return result

    def close(self):
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
            self.query = None
