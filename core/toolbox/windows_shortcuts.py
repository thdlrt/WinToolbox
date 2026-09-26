"""Create Unicode shell links through IShellLinkW/IPersistFile, without a shell."""
import ctypes
from ctypes import wintypes
import uuid


def create_shortcut(link, target, arguments=''):
    # WScript.Shell.Save uses a legacy filename path on some Windows locales.
    # IPersistFile accepts UTF-16 for the whole path, including the user profile.
    class Guid(ctypes.Structure):
        _fields_ = [('data1', wintypes.DWORD), ('data2', wintypes.WORD),
                    ('data3', wintypes.WORD), ('data4', ctypes.c_ubyte * 8)]
    def guid(value):
        return Guid.from_buffer_copy(uuid.UUID(value).bytes_le)
    hresult = ctypes.c_long
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = hresult
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(Guid), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = hresult
    def checked(result):
        if result < 0:
            raise OSError(f'Windows 快捷方式操作失败（HRESULT 0x{result & 0xffffffff:08X}）')
    def method(pointer, index, *types):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(hresult, ctypes.c_void_p, *types)(table[index])
    initialized = ole.CoInitializeEx(None, 2)
    # An existing MTA is valid; balance only this call's successful initialization.
    if initialized != -2147417850:  # RPC_E_CHANGED_MODE
        checked(initialized)
    shell = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        clsid = guid('00021401-0000-0000-C000-000000000046')
        iid = guid('000214F9-0000-0000-C000-000000000046')  # IShellLinkW
        checked(ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(shell)))
        checked(method(shell, 20, wintypes.LPCWSTR)(shell, str(target)))
        checked(method(shell, 11, wintypes.LPCWSTR)(shell, arguments))
        checked(method(shell, 9, wintypes.LPCWSTR)(shell, str(target.parent)))
        iid = guid('0000010B-0000-0000-C000-000000000046')  # IPersistFile
        checked(method(shell, 0, ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p))(
            shell, ctypes.byref(iid), ctypes.byref(persist)))
        checked(method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(persist, str(link), True))
    finally:
        if persist: method(persist, 2)(persist)
        if shell: method(shell, 2)(shell)
        if initialized >= 0: ole.CoUninitialize()
