"""Local clipboard history. Payloads never enter jobs, sync or event messages."""
import base64
import ctypes as c
import hashlib
import io
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

MAX_BYTES = 32 * 1024 * 1024


class WindowsClipboard:
    def __init__(self):
        from ctypes import wintypes as w
        self.u = c.WinDLL('user32', use_last_error=True)
        self.k = c.WinDLL('kernel32', use_last_error=True)
        signatures = [(self.u, 'GetClipboardSequenceNumber', [], w.DWORD),
            (self.u, 'OpenClipboard', [w.HWND], w.BOOL), (self.u, 'CloseClipboard', [], w.BOOL),
            (self.u, 'EmptyClipboard', [], w.BOOL), (self.u, 'IsClipboardFormatAvailable', [w.UINT], w.BOOL),
            (self.u, 'GetClipboardData', [w.UINT], w.HANDLE), (self.u, 'SetClipboardData', [w.UINT, w.HANDLE], w.HANDLE),
            (self.u, 'RegisterClipboardFormatW', [w.LPCWSTR], w.UINT),
            (self.u, 'CreateWindowExW', [w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,c.c_int,c.c_int,c.c_int,c.c_int,w.HWND,w.HMENU,w.HINSTANCE,c.c_void_p], w.HWND),
            (self.u, 'DestroyWindow', [w.HWND], w.BOOL),
            (self.k, 'GlobalLock', [w.HGLOBAL], c.c_void_p), (self.k, 'GlobalUnlock', [w.HGLOBAL], w.BOOL),
            (self.k, 'GlobalSize', [w.HGLOBAL], c.c_size_t), (self.k, 'GlobalAlloc', [w.UINT,c.c_size_t], w.HGLOBAL),
            (self.k, 'GlobalFree', [w.HGLOBAL], w.HGLOBAL)]
        for lib, name, args, result in signatures:
            fn = getattr(lib, name); fn.argtypes = args; fn.restype = result
        self.png = self.u.RegisterClipboardFormatW('PNG')
        self.excluded = self.u.RegisterClipboardFormatW('ExcludeClipboardContentFromMonitorProcessing')

    def sequence(self):
        return self.u.GetClipboardSequenceNumber()

    @contextmanager
    def opened(self):
        hwnd = self.u.CreateWindowExW(0, 'STATIC', 'WinToolbox Clipboard', 0, 0, 0, 0, 0, None, None, None, None)
        if not hwnd:
            raise OSError('无法创建剪贴板窗口')
        try:
            for _ in range(6):
                if self.u.OpenClipboard(hwnd): break
                time.sleep(.02)
            else: raise OSError('剪贴板正被其他程序占用，请稍后重试')
            try: yield
            finally: self.u.CloseClipboard()
        finally: self.u.DestroyWindow(hwnd)

    def read_bytes(self, fmt):
        handle = self.u.GetClipboardData(fmt)
        if not handle: raise OSError('无法读取剪贴板内容')
        size = self.k.GlobalSize(handle)
        if size > MAX_BYTES: raise ValueError('剪贴板内容超过 32 MB，已跳过')
        ptr = self.k.GlobalLock(handle)
        if not ptr: raise OSError('无法访问剪贴板内容')
        try: return c.string_at(ptr, size)
        finally: self.k.GlobalUnlock(handle)

    def read(self):
        with self.opened():
            seq = self.sequence()
            if self.u.IsClipboardFormatAvailable(self.excluded): return seq, None
            for fmt in (self.png, 8, 17):
                if self.u.IsClipboardFormatAvailable(fmt): return seq, ('image', self.read_bytes(fmt))
            if self.u.IsClipboardFormatAvailable(13):
                return seq, ('text', self.read_bytes(13).decode('utf-16-le', errors='replace').split('\0', 1)[0])
            return seq, None

    def write(self, kind, payload):
        if kind == 'text': fmt, raw = 13, (payload + '\0').encode('utf-16-le')
        else:
            from PIL import Image
            out = io.BytesIO()
            with Image.open(io.BytesIO(payload)) as im: im.convert('RGB').save(out, 'BMP')
            fmt, raw = 8, out.getvalue()[14:]
        handle = self.k.GlobalAlloc(2, len(raw))
        if not handle: raise OSError('剪贴板内存分配失败')
        try:
            ptr = self.k.GlobalLock(handle)
            if not ptr: raise OSError('剪贴板内存锁定失败')
            try: c.memmove(ptr, raw, len(raw))
            finally: self.k.GlobalUnlock(handle)
            with self.opened():
                if not self.u.EmptyClipboard() or not self.u.SetClipboardData(fmt, handle): raise OSError('写入剪贴板失败')
                handle = None
                if kind == 'image':
                    # Keep alpha for PNG-aware applications alongside standard DIB.
                    png_handle = self.k.GlobalAlloc(2, len(payload))
                    if not png_handle: raise OSError('PNG 剪贴板内存分配失败')
                    try:
                        ptr = self.k.GlobalLock(png_handle)
                        if not ptr: raise OSError('PNG 剪贴板内存锁定失败')
                        try: c.memmove(ptr, payload, len(payload))
                        finally: self.k.GlobalUnlock(png_handle)
                        if not self.u.SetClipboardData(self.png, png_handle): raise OSError('PNG 写入剪贴板失败')
                        png_handle = None
                    finally:
                        if png_handle: self.k.GlobalFree(png_handle)
                return self.sequence()
        finally:
            if handle: self.k.GlobalFree(handle)


class ClipboardHistory:
    def __init__(self, data_dir, adapter=None, clock=time.time):
        self.root = data_dir / 'clipboard-local'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.clock, self.adapter = clock, adapter
        self.stop = threading.Event()
        self.thread = None
        self.error = ''
        self.seq = adapter.sequence() if adapter else 0
        self.db = sqlite3.connect(self.root / 'history.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('casefold', 1, lambda text: text.casefold())
        self.last_cleanup = 0
        self.db.executescript('''PRAGMA user_version=1;
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, days INTEGER, enabled INTEGER);
            INSERT OR IGNORE INTO config VALUES(1,3,1);
            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, kind TEXT, text TEXT, image BLOB, thumbnail TEXT, width INTEGER, height INTEGER, copied_at REAL);
            CREATE INDEX IF NOT EXISTS copied ON items(copied_at DESC);''')
        self.cleanup()

    def config(self):
        row = self.db.execute('SELECT * FROM config WHERE id=1').fetchone()
        return {'days': row['days'], 'enabled': bool(row['enabled']), 'available': self.adapter is not None, 'error': self.error}

    def cleanup(self):
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('DELETE FROM items WHERE copied_at<=?', (self.clock()-self.config()['days']*86400,))
        self.db.commit()

    def capture(self, kind, payload):
        with self.lock:
            text, blob, thumb, width, height = '', None, '', 0, 0
            if kind == 'text':
                if not payload.strip(): return
                if len(payload.encode('utf-8')) > MAX_BYTES: raise ValueError('文字超过 32 MB，已跳过')
                text = payload
                raw = text.encode('utf-8')
            else:
                from PIL import Image
                with Image.open(io.BytesIO(payload)) as im:
                    if im.width * im.height > 25_000_000: raise ValueError('图片超过 2500 万像素，已跳过')
                    im = im.convert('RGBA'); width, height = im.size
                    out = io.BytesIO(); im.save(out, 'PNG'); blob = out.getvalue()
                    if len(blob) > MAX_BYTES: raise ValueError('图片超过 32 MB，已跳过')
                    im.thumbnail((240, 160)); out = io.BytesIO(); im.save(out, 'PNG')
                    thumb = base64.b64encode(out.getvalue()).decode('ascii')
                raw = blob
            ident = hashlib.sha256(kind.encode()+raw).hexdigest()
            self.db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?)', (ident,kind,text,blob,thumb,width,height,self.clock()))
            self.db.commit()
            return ident

    def tick(self):
        with self.lock:
            if self.clock() - self.last_cleanup >= 60:
                self.cleanup()
                self.last_cleanup = self.clock()
            if not self.adapter or not self.config()['enabled']: return
            if self.adapter.sequence() == self.seq: return
            seq, item = self.adapter.read()
            try:
                if item: self.capture(*item)
            finally: self.seq = seq
            self.error = ''

    def start(self):
        if not self.adapter: return
        def loop():
            while not self.stop.wait(.5):
                try: self.tick()
                except Exception as exc: self.error = str(exc)
        self.thread = threading.Thread(target=loop, name='clipboard-history', daemon=True)
        self.thread.start()

    def call(self, action, p):
        with self.lock:
            self.cleanup()
            if action == 'settings':
                if 'enabled' in p and type(p['enabled']) is not bool:
                    raise ValueError('启用状态必须为布尔值')
                if 'days' in p:
                    days = p['days']
                    if type(days) is not int or not 1 <= days <= 365: raise ValueError('保留天数应为 1–365 的整数')
                    self.db.execute('UPDATE config SET days=? WHERE id=1', (days,))
                if 'enabled' in p:
                    self.db.execute('UPDATE config SET enabled=? WHERE id=1', (int(p['enabled']),))
                    if self.adapter: self.seq = self.adapter.sequence()
                self.db.commit(); self.cleanup()
                return self.config()
            if action == 'list':
                kind = p.get('kind', 'all'); query = str(p.get('query', '')).casefold()
                if kind not in ('all','text','image'): raise ValueError('未知分类')
                offset = max(0, int(p.get('offset', 0)))
                rows = self.db.execute('SELECT id,kind,substr(text,1,500) preview,thumbnail,width,height,copied_at FROM items WHERE (?=\'all\' OR kind=?) AND instr(casefold(text),?)>0 ORDER BY copied_at DESC LIMIT 51 OFFSET ?', (kind,kind,query,offset)).fetchall()
                return {'items': [dict(r) for r in rows[:50]], 'more': len(rows)>50, 'config': self.config()}
            if action == 'clear':
                if p.get('confirm') is not True: raise ValueError('请确认清空历史')
                self.db.execute('DELETE FROM items'); self.db.commit(); return {'ok':True}
            row = self.db.execute('SELECT * FROM items WHERE id=?', (p.get('id'),)).fetchone()
            if not row: raise ValueError('记录已过期或已删除')
            if action == 'delete':
                self.db.execute('DELETE FROM items WHERE id=?',(row['id'],)); self.db.commit(); return {'ok':True}
            if action == 'get':
                return {'kind':row['kind'], 'text':row['text'], 'image':base64.b64encode(row['image']).decode() if row['image'] else ''}
            if action == 'copy':
                if not self.adapter: raise RuntimeError('剪贴板仅支持 Windows 桌面')
                self.seq = self.adapter.write(row['kind'], row['text'] if row['kind']=='text' else row['image'])
                return {'ok':True}
            raise ValueError('未知操作')

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=2)
        with self.lock:
            if self.db:
                self.db.close(); self.db = None


def register(app):
    service = ClipboardHistory(app.data_dir, WindowsClipboard() if os.name == 'nt' else None)
    service.db.create_function('casefold', 1, lambda text: text.casefold())
    app.clipboard_history = service
    for action in ('settings','list','get','copy','delete','clear'):
        app.register('clipboard.'+action, lambda p, a=action: service.call(a,p))
