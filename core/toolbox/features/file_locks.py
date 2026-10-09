"""Windows file owners; never close foreign handles or delete selected files."""
import ctypes as C
import os
from ctypes import wintypes as W
from pathlib import Path

MAX_ITEMS = 5000
REGISTRY_ROOTS = [r'Software\Classes\*\shell\WinToolboxFileLocks',
                  r'Software\Classes\Directory\shell\WinToolboxFileLocks']


class IOStatus(C.Structure):
    _fields_ = [('status', C.c_void_p), ('information', C.c_size_t)]


class ProcessIds(C.Structure):
    _fields_ = [('count', W.ULONG), ('ids', C.c_size_t * 4096)]


class ServiceStatus(C.Structure):
    _fields_ = [(name, W.DWORD) for name in ('type', 'state', 'accepted', 'exit_code', 'specific_exit',
                                           'checkpoint', 'wait_hint', 'pid', 'flags')]


class ServiceEntry(C.Structure):
    _fields_ = [('name', W.LPWSTR), ('display', W.LPWSTR), ('status', ServiceStatus)]


class UniqueProcess(C.Structure):
    _fields_ = [('pid', W.DWORD), ('start', W.FILETIME)]


class ProcessInfo(C.Structure):
    _fields_ = [('process', UniqueProcess), ('name', W.WCHAR * 256),
                ('service', W.WCHAR * 64), ('kind', C.c_int), ('status', W.ULONG),
                ('session', W.DWORD), ('restartable', W.BOOL)]


def filetime(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class WindowsLocks:
    def __init__(self):
        if os.name != 'nt':
            raise ValueError('文件占用检查仅支持 Windows')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.user = C.WinDLL('user32', use_last_error=True)
        self.rm = C.WinDLL('rstrtmgr', use_last_error=True)
        self.nt = C.WinDLL('ntdll', use_last_error=True)
        self.nt.NtQueryInformationFile.argtypes = [W.HANDLE, C.POINTER(IOStatus), C.c_void_p, W.ULONG, C.c_int]
        self.nt.NtQueryInformationFile.restype = C.c_long
        signatures = {
            'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            'CloseHandle': ([W.HANDLE], W.BOOL),
            'GetProcessTimes': ([W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            'QueryFullProcessImageNameW': ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
            'IsProcessCritical': ([W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
            'TerminateProcess': ([W.HANDLE, W.UINT], W.BOOL),
            'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
            'CreateFileW': ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
            'ProcessIdToSessionId': ([W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.kernel, name); fn.argtypes = args; fn.restype = result
        for name, args in {
            'RmStartSession': [C.POINTER(W.DWORD), W.DWORD, W.LPWSTR],
            'RmRegisterResources': [W.DWORD, W.UINT, C.POINTER(W.LPCWSTR), W.UINT, C.POINTER(UniqueProcess), W.UINT, C.POINTER(W.LPCWSTR)],
            'RmGetList': [W.DWORD, C.POINTER(W.UINT), C.POINTER(W.UINT), C.POINTER(ProcessInfo), C.POINTER(W.DWORD)],
            'RmEndSession': [W.DWORD],
        }.items():
            fn = getattr(self.rm, name); fn.argtypes = args; fn.restype = W.DWORD

    def process(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return {'image': '', 'critical': True, 'query_error': C.get_last_error()}
        try:
            start, exit_time, kernel, user = [W.FILETIME() for _ in range(4)]
            critical = W.BOOL()
            if not self.kernel.GetProcessTimes(handle, C.byref(start), C.byref(exit_time), C.byref(kernel), C.byref(user)):
                return {'image': '', 'critical': True, 'query_error': C.get_last_error()}
            known = self.kernel.IsProcessCritical(handle, C.byref(critical))
            buffer, size = C.create_unicode_buffer(32768), W.DWORD(32768)
            image = buffer.value if self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(size)) else ''
            return {'image': image, 'critical': not known or bool(critical.value), 'start': str(filetime(start))}
        finally:
            self.kernel.CloseHandle(handle)

    def owners(self, paths):
        session, key = W.DWORD(), C.create_unicode_buffer(33)
        def check(code):
            if code:
                raise OSError(code, 'Windows 占用查询失败：' + C.FormatError(code).strip())
        check(self.rm.RmStartSession(C.byref(session), 0, key))
        try:
            resources = (W.LPCWSTR * len(paths))(*paths)
            check(self.rm.RmRegisterResources(session, len(paths), resources, 0, None, 0, None))
            capacity = 0
            for _ in range(8):
                needed, count, reason = W.UINT(), W.UINT(capacity), W.DWORD()
                rows = (ProcessInfo * capacity)() if capacity else None
                code = self.rm.RmGetList(session, C.byref(needed), C.byref(count), rows, C.byref(reason))
                if code == 234:
                    capacity = needed.value
                    if capacity > 4096:
                        raise ValueError('占用进程过多，请分批检查')
                    continue
                check(code)
                results = []
                for row in (rows or [])[:count.value]:
                    pid, start = row.process.pid, str(filetime(row.process.start))
                    detail = self.process(pid)
                    blocked = row.kind in (3, 4, 1000) or bool(row.service) or pid in (0, 4, os.getpid(), os.getppid()) or detail.get('critical') or detail.get('start') != start
                    if Path(detail.get('image', '')).name.lower() in ('explorer.exe', 'wintoolbox.exe'):
                        blocked = True
                    results.append({'pid': pid, 'start': start, 'key': f'{pid}:{start}', 'name': row.name,
                                    'service': row.service, 'kind': row.kind, 'image': detail.get('image', ''),
                                    'can_release': not blocked,
                                    'reason': '系统、服务、工具箱或无法安全核对的进程，请手动处理' if blocked else ''})
                return results
            raise ValueError('占用进程不断变化，请重新检查')
        finally:
            self.rm.RmEndSession(session)

    def service_pids(self):
        advapi = C.WinDLL('advapi32', use_last_error=True)
        advapi.OpenSCManagerW.argtypes = [W.LPCWSTR, W.LPCWSTR, W.DWORD]
        advapi.OpenSCManagerW.restype = W.HANDLE
        advapi.CloseServiceHandle.argtypes = [W.HANDLE]
        advapi.EnumServicesStatusExW.argtypes = [W.HANDLE, C.c_int, W.DWORD, W.DWORD, C.c_void_p,
                                                W.DWORD, C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.DWORD), W.LPCWSTR]
        advapi.EnumServicesStatusExW.restype = W.BOOL
        manager = advapi.OpenSCManagerW(None, None, 4)
        if not manager:
            raise OSError(C.get_last_error(), '无法核对系统服务，目录占用进程仅供查看')
        try:
            result, resume = set(), W.DWORD()
            buffer = C.create_string_buffer(256 * 1024)
            for _ in range(32):
                needed, returned = W.DWORD(), W.DWORD()
                ok = advapi.EnumServicesStatusExW(manager, 0, 0x30, 3, buffer, len(buffer),
                                                 C.byref(needed), C.byref(returned), C.byref(resume), None)
                code = C.get_last_error() if not ok else 0
                if code not in (0, 234):
                    raise OSError(code, '无法核对系统服务，目录占用进程仅供查看')
                rows = C.cast(buffer, C.POINTER(ServiceEntry))
                result.update(rows[i].status.pid for i in range(returned.value) if rows[i].status.pid)
                if ok:
                    return result
            raise ValueError('系统服务列表不断变化，请重新检查')
        finally:
            advapi.CloseServiceHandle(manager)

    def directory_pids(self, path):
        # FILE_READ_ATTRIBUTES does not request file data and can inspect shared locks.
        handle = self.kernel.CreateFileW(path, 0x80, 7, None, 3, 0x02000000 | 0x00200000, None)
        if handle == C.c_void_p(-1).value:
            raise OSError(C.get_last_error(), '无法查询目录占用：' + path)
        try:
            rows, status = ProcessIds(), IOStatus()
            code = self.nt.NtQueryInformationFile(handle, C.byref(status), C.byref(rows), C.sizeof(rows), 47)
            if code != 0 or rows.count > 4096:
                raise ValueError(f'此目录的文件系统无法完整查询直接占用（0x{code & 0xffffffff:08X}）：{path}')
            return list(rows.ids[:rows.count])
        finally:
            self.kernel.CloseHandle(handle)

    def directory_process(self, pid, service_pids):
        detail = self.process(pid)
        session, own_session = W.DWORD(), W.DWORD()
        sessions_known = self.kernel.ProcessIdToSessionId(pid, C.byref(session)) and self.kernel.ProcessIdToSessionId(os.getpid(), C.byref(own_session))
        name = Path(detail.get('image', '')).name
        blocked = (service_pids is None or pid in service_pids or not sessions_known or session.value == 0 or
                   session.value != own_session.value or detail.get('critical') or not detail.get('start') or
                   pid in (0, 4, os.getpid(), os.getppid()) or name.lower() in ('explorer.exe', 'wintoolbox.exe', 'svchost.exe'))
        start = detail.get('start', '')
        return {'pid': pid, 'start': start, 'key': f'{pid}:{start}', 'name': name or f'进程 {pid}',
                'image': detail.get('image', ''), 'service': '系统服务' if service_pids and pid in service_pids else '',
                'kind': 'directory', 'can_release': not blocked,
                'reason': '系统、服务、工具箱或无法安全核对的进程，请手动处理' if blocked else ''}

    def probe(self, path):
        # DELETE access only: obtain/close a handle without marking the file for deletion.
        handle = self.kernel.CreateFileW(path, 0x10000, 7, None, 3, 0x02000000 | 0x00200000, None)
        if handle == C.c_void_p(-1).value:
            code = C.get_last_error()
            status = 'blocked' if code in (32, 33) else 'denied' if code == 5 else 'missing' if code in (2, 3) else 'error'
            return {'path': path, 'status': status, 'error': code, 'message': C.FormatError(code).strip()}
        self.kernel.CloseHandle(handle)
        return {'path': path, 'status': 'available', 'message': '当前可取得删除权限；未删除文件'}

    def release(self, process, mode):
        if not process['can_release']:
            raise ValueError(process['reason'])
        # Hold one process handle throughout identity validation and termination (PID reuse safe).
        access = 0x1000 | 0x100000 | (1 if mode == 'terminate' else 0)
        handle = self.kernel.OpenProcess(access, False, process['pid'])
        if not handle:
            raise OSError(C.get_last_error(), '无法操作此进程，请手动关闭程序或以管理员身份重试')
        try:
            start, exit_time, kernel, user = [W.FILETIME() for _ in range(4)]
            critical = W.BOOL()
            if not self.kernel.GetProcessTimes(handle, C.byref(start), C.byref(exit_time), C.byref(kernel), C.byref(user)) or str(filetime(start)) != process['start']:
                raise ValueError('进程已退出或身份变化，请重新检查')
            if not self.kernel.IsProcessCritical(handle, C.byref(critical)) or critical.value:
                raise ValueError('不能结束关键系统进程')
            if mode == 'terminate':
                if not self.kernel.TerminateProcess(handle, 1):
                    raise OSError(C.get_last_error(), '结束进程失败，请手动处理或以管理员身份重试')
                if self.kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise ValueError('结束请求已发送，但进程尚未退出，请重新检查')
                return '进程已结束'
            callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
            self.user.EnumWindows.argtypes = [callback_type, W.LPARAM]
            self.user.EnumWindows.restype = W.BOOL
            self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
            self.user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
            self.user.PostMessageW.restype = W.BOOL
            sent = []
            @callback_type
            def visit(window, _):
                pid = W.DWORD(); self.user.GetWindowThreadProcessId(window, C.byref(pid))
                if pid.value == process['pid'] and self.user.PostMessageW(window, 0x10, 0, 0):
                    sent.append(window)
                return True
            self.user.EnumWindows(visit, 0)
            if not sent:
                raise ValueError('此进程没有可关闭的窗口，或权限不足；可手动退出或确认强制结束')
            self.kernel.WaitForSingleObject(handle, 1500)
            return '已请求关闭；如出现保存提示，请先处理'
        finally:
            self.kernel.CloseHandle(handle)


def enumerate_paths(paths, cancel):
    items, seen, warnings = [], set(), []
    pending = list(reversed(paths))
    while pending:
        cancel()
        path = Path(pending.pop())
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        if len(items) >= MAX_ITEMS:
            warnings.append(f'已达到 {MAX_ITEMS} 个项目的扫描上限，请选择更小的文件夹')
            break
        seen.add(key)
        items.append(str(path))
        try:
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                warnings.append('未跟随符号链接或联接：' + str(path))
                continue
            if path.is_dir():
                with os.scandir(path) as entries:
                    for entry in entries:
                        cancel()
                        if len(pending) + len(items) >= MAX_ITEMS:
                            warnings.append(f'已达到 {MAX_ITEMS} 个项目的扫描上限，请选择更小的文件夹')
                            break
                        pending.append(entry.path)
        except OSError as exc:
            warnings.append('无法完整读取文件夹：' + str(path) + '；' + str(exc))
    return items, list(dict.fromkeys(warnings))


def scan(paths, cancel=lambda: None):
    if not paths or len(paths) > 100 or any(not Path(p).is_absolute() for p in paths):
        raise ValueError('请选择 1–100 个文件或文件夹')
    items, warnings = enumerate_paths(paths, cancel)
    api = WindowsLocks()
    cancel()
    existing = [p for p in items if Path(p).is_file() and not Path(p).is_symlink()]
    owners = {}
    for offset in range(0, len(existing), 256):
        cancel()
        for owner in api.owners(existing[offset:offset + 256]):
            owners[owner['key']] = owner
    directories = [p for p in items if Path(p).is_dir() and not Path(p).is_symlink() and not getattr(Path(p), 'is_junction', lambda: False)()]
    service_pids = None
    if directories:
        try:
            service_pids = api.service_pids()
        except (OSError, ValueError) as exc:
            warnings.append(str(exc))
    directory_owners = {}
    for directory in directories:
        cancel()
        try:
            for pid in api.directory_pids(directory):
                if pid == os.getpid():
                    continue
                if pid not in directory_owners:
                    directory_owners[pid] = api.directory_process(pid, service_pids)
                owner = directory_owners[pid]
                previous = owners.get(owner['key'])
                if previous:
                    previous['can_release'] = previous['can_release'] and owner['can_release']
                    if not owner['can_release']: previous['reason'] = owner['reason']
                else:
                    owners[owner['key']] = owner
        except (OSError, ValueError) as exc:
            warnings.append(str(exc))
    files = []
    for path in items:
        cancel(); files.append(api.probe(path))
    selected = {os.path.normcase(p) for p in paths}
    issues = [f for f in files if f['status'] not in ('available', 'missing')]
    display = []
    for item in files:
        if os.path.normcase(item['path']) not in selected:
            continue
        value = dict(item)
        if Path(item['path']).is_dir():
            descendants = [f for f in issues if Path(item['path']) in Path(f['path']).parents]
            if value['status'] == 'available' and descendants:
                problem = next((f for f in descendants if f['status'] == 'blocked'), descendants[0])
                value.update(status=problem['status'], message='子项阻止删除：' + problem['path'] + '；' + problem['message'])
            elif value['status'] == 'available' and warnings:
                value.update(status='incomplete', message='已检查的子项未发现阻塞，但扫描不完整')
            elif value['status'] == 'available':
                value['message'] = '目录及已扫描子项未发现删除访问阻塞；未删除文件夹'
        display.append(value)
    display.extend(f for f in issues if os.path.normcase(f['path']) not in selected)
    warnings = list(dict.fromkeys(warnings))
    if len(warnings) > 30:
        warnings = warnings[:30] + [f'另有 {len(warnings) - 30} 条未完整检查的提示，请缩小选择范围']
    return {'processes': list(owners.values()), 'files': display[:200], 'scanned_count': len(items),
            'issue_count': len(issues), 'warnings': warnings, 'partial': bool(warnings),
            'notice': '已递归检查文件夹及子项，并查询目录直接占用。未列出的驱动或内核占用需手动处理。' if directories else '列出 Windows Restart Manager 能识别的占用。未列出的驱动或内核占用需手动处理。'}


def context_menu(enabled=None):
    if os.name != 'nt':
        return {'enabled': False, 'supported': False}
    import winreg
    from .relay import notify_shell_associations
    exe = Path(os.environ.get('WINTOOLBOX_APP_EXE', ''))
    valid = exe.is_absolute() and exe.is_file() and exe.suffix.lower() == '.exe'
    command = f'"{exe}" --file-locks "%1"'
    if enabled is not None:
        if type(enabled) is not bool or not valid:
            raise ValueError('请在已打包的 Windows 工具箱中设置右键菜单')
        for root in REGISTRY_ROOTS:
            if enabled:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
                    for name, value in [('', '查看并解除文件占用'), ('MUIVerb', '查看并解除文件占用'), ('Icon', str(exe)), ('MultiSelectModel', 'Single')]:
                        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r'\command') as key:
                    winreg.SetValueEx(key, '', 0, winreg.REG_SZ, command)
            else:
                for name in (root + r'\command', root):
                    try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)
                    except FileNotFoundError: pass
        notify_shell_associations()
    active = []
    for root in REGISTRY_ROOTS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root + r'\command') as key:
                active.append(winreg.QueryValueEx(key, '')[0] == command)
        except FileNotFoundError:
            active.append(False)
    return {'enabled': all(active), 'supported': valid}
