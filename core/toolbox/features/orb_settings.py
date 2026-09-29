"""Ordered, bounded shortcuts for the floating toolbox."""
import json
import threading
from ..settings import atomic_json

DEFAULT_ACTIONS = ['clean', 'relay', 'subtitle-toggle', 'home']
ALLOWED_ACTIONS = frozenset(DEFAULT_ACTIONS + ['ram', 'captions', 'filesync', 'memory', 'media', 'live', 'practice', 'phonetics', 'expenses', 'shizuku', 'fnconnect', 'gpu', 'codex', 'files', 'scripts', 'plugins', 'settings'])


class OrbSettings:
    def __init__(self, app):
        self.app = app
        self.path = app.data_dir / 'orb-settings.json'
        self.lock = threading.RLock()

    def get(self, _=None):
        with self.lock:
            auto_hide = True
            try:
                value = json.loads(self.path.read_text('utf-8'))
                auto_hide = value.get('oled_auto_hide', True) is not False
                actions = value.get('actions')
                if isinstance(actions, list): actions = list(dict.fromkeys('scripts' if a == 'files' else a for a in actions))
                self.validate(actions)
            except (OSError, ValueError, TypeError, AttributeError): actions = DEFAULT_ACTIONS.copy()
            return {'actions': actions, 'oled_auto_hide': auto_hide}

    @staticmethod
    def validate(actions):
        if not isinstance(actions, list) or not 1 <= len(actions) <= 6:
            raise ValueError('请选择 1–6 个快捷功能')
        if any(not isinstance(a, str) or a not in ALLOWED_ACTIONS for a in actions):
            raise ValueError('包含不支持的快捷功能')
        if len(set(actions)) != len(actions): raise ValueError('快捷功能不能重复')

    def save(self, params):
        actions = params.get('actions')
        self.validate(actions)
        auto_hide = params.get('oled_auto_hide', self.get()['oled_auto_hide'])
        if not isinstance(auto_hide, bool): raise ValueError('OLED 自动隐去开关无效')
        value = {'actions': actions.copy(), 'oled_auto_hide': auto_hide}
        with self.lock: atomic_json(self.path, value)
        self.app.emit('orb.settings.changed', **value)
        return value


def register(app):
    service = OrbSettings(app)
    app.register('orb.settings.get', service.get)
    app.register('orb.settings.save', service.save)
