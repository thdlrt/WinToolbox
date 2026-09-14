"""Small, portable phonetics-course progress under the existing practice data."""
import copy
import json
import threading
from pathlib import Path

from ..settings import atomic_json


DEFAULT_STATE = {'done': [], 'custom': '', 'checks': {}, 'best': 0}


def validate_state(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_STATE):
        raise ValueError('音标进度必须完整包含 done、custom、checks 和 best')
    done = value['done']
    if (not isinstance(done, list) or len(done) > 5
            or any(type(day) is not int or not 0 <= day <= 4 for day in done)
            or len(set(done)) != len(done)):
        raise ValueError('已完成课程必须为不重复的 0 到 4 整数列表')
    if not isinstance(value['custom'], str) or len(value['custom']) > 20000:
        raise ValueError('自定义练习文本最多 20000 个字符')
    checks = value['checks']
    if (not isinstance(checks, dict) or len(checks) > 100
            or any(not isinstance(key, str) or not 1 <= len(key) <= 80
                   or any(ord(char) < 32 or ord(char) == 127 for char in key)
                   or type(checked) is not bool for key, checked in checks.items())):
        raise ValueError('检查项最多 100 个，名称需为 1 到 80 个字符，状态必须为布尔值')
    if type(value['best']) is not int or not 0 <= value['best'] <= 6:
        raise ValueError('最佳成绩必须为 0 到 6 的整数')
    # Reject invalid lone UTF-16 surrogates before any file operation. Normal
    # Unicode text, emoji and newlines in custom exercises remain supported.
    try:
        json.dumps(value, ensure_ascii=False).encode('utf-8')
    except UnicodeError:
        raise ValueError('音标进度包含无效的文本字符') from None
    return copy.deepcopy(value)


def register(app):
    path = app.data_dir / 'practice' / 'phonetics-state.json'
    lock = threading.RLock()

    def check_path():
        for part in (path, path.parent):
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                raise ValueError('音标进度不能存储在符号链接或目录联接中')
        if not path.resolve().is_relative_to(Path(app.data_dir).resolve()):
            raise ValueError('音标进度路径超出应用数据目录')

    def load():
        check_path()
        if not path.exists():
            return copy.deepcopy(DEFAULT_STATE)
        try:
            if not path.is_file() or path.stat().st_size > 256 * 1024:
                raise ValueError('文件大小或类型无效')
            return validate_state(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError, UnicodeError):
            raise ValueError('音标学习进度文件损坏或无法读取，请检查备份；原文件未覆盖') from None

    def get(_):
        with lock:
            return load()

    def update(params):
        if set(params) != {'state'}:
            raise ValueError('请通过 state 提交完整音标学习进度')
        state = validate_state(params['state'])
        with lock:
            # Do not silently replace corrupt saved progress with an apparently
            # successful fresh state. Normal reads and writes share App.data_lock
            # with backups/restores through the standard synchronous RPC gate.
            load()
            atomic_json(path, state)
            return state

    app.register('phonetics.state.get', get)
    app.register('phonetics.state.update', update)
