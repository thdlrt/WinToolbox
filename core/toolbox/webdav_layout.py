"""One shared connection and fixed, application-owned service directories."""
import json
import hashlib
import threading
from pathlib import Path
from urllib.parse import quote

from .settings import atomic_json

SERVICES = {'config_backups': 'config-backups/windows', 'ledger': 'ledger-v1',
            'relay': 'file-relay', 'project_memory': 'project-memory'}
MIGRATION_LOCK = threading.RLock()


def migration_config(source, current):
    from .features.webdav import normalized_url
    value = dict(source)
    if normalized_url(source['url']) == normalized_url(current['url']) and source.get('username') == current.get('username'):
        value['password_dpapi'] = current.get('password_dpapi', '')
    return value


def remember_ledger_source(data_dir, previous, destination):
    """Private migration metadata stays outside ledger and portable config backups."""
    path = Path(data_dir) / 'webdav-migrations.json'
    identity = [previous.get(k) for k in ('url', 'username', 'remote_path')] + [destination.get(k) for k in ('url', 'username', 'remote_path')]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    with MIGRATION_LOCK:
        data = json.loads(path.read_text('utf-8')) if path.exists() else {}
        data.setdefault('ledger_sources', {})[key] = {'config': {k: previous[k] for k in ('url', 'username', 'password_dpapi', 'remote_path')},
                                                     'pending': True, 'error': None}
        atomic_json(path, data)


def ledger_sources(data_dir):
    path = Path(data_dir) / 'webdav-migrations.json'
    with MIGRATION_LOCK:
        data = json.loads(path.read_text('utf-8')) if path.exists() else {}
        return data.get('ledger_sources', {})


def ledger_source_result(data_dir, key, error=None):
    path = Path(data_dir) / 'webdav-migrations.json'
    with MIGRATION_LOCK:
        data = json.loads(path.read_text('utf-8')) if path.exists() else {}
        if key in data.get('ledger_sources', {}):
            data['ledger_sources'][key].update(pending=bool(error), error=error)
            atomic_json(path, data)


def shared_config(data_dir):
    path = Path(data_dir) / 'webdav.json'
    saved = json.loads(path.read_text('utf-8')) if path.exists() else {}
    return {'url': '', 'username': '', 'remote_path': 'WinToolbox', **saved}


def service_config(config, service):
    from .features.webdav import normalized_path
    root = normalized_path(config.get('remote_path', 'WinToolbox'))
    return {**config, 'shared_root': root, 'remote_path': root + '/' + SERVICES[service]}


def service_paths(config):
    return {key: service_config(config, key)['remote_path'] for key in SERVICES}


def ensure_service_directory(remote):
    """Create only selected root and fixed descendants; require its parent."""
    root = remote.config.get('shared_root')
    if not root:
        return False
    parts = remote.config['remote_path'].split('/')
    root_parts = root.split('/')
    if parts[:len(root_parts)] != root_parts:
        raise ValueError('WebDAV 服务目录不在共享根目录中')
    base = remote.config['url'].rstrip('/') + '/'
    parent = base + '/'.join(quote(part, safe='') for part in root_parts[:-1])
    parent = parent.rstrip('/') + '/'
    xml = remote.xml(parent, 0)
    if xml is None or not xml.findall('.//{DAV:}collection'):
        raise ValueError('共享根目录的父文件夹不存在，请先在网盘创建')
    for end in range(len(root_parts), len(parts) + 1):
        url = base + '/'.join(quote(part, safe='') for part in parts[:end]) + '/'
        existing = remote.xml(url, 0)
        if existing is None:
            remote.checked(remote.request('MKCOL', url), (201, 405))
            existing = remote.xml(url, 0)
        if existing is None or not existing.findall('.//{DAV:}collection'):
            raise ValueError('WebDAV 服务目录被文件占用或无法访问')
    return True


def promote_legacy_connection(data_dir):
    """Keep credentials when relay was previously the only configured service."""
    data_dir = Path(data_dir)
    shared = shared_config(data_dir)
    if shared.get('url') and shared.get('username') and shared.get('password_dpapi'):
        return
    legacy_path = data_dir / 'relay-connection.json'
    if not legacy_path.exists():
        return
    legacy = json.loads(legacy_path.read_text('utf-8'))
    if legacy.get('url') and legacy.get('username') and legacy.get('password_dpapi'):
        for field in ('url', 'username', 'password_dpapi'):
            shared[field] = legacy[field]
        atomic_json(data_dir / 'webdav.json', shared)
