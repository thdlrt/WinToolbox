"""Project memory RPC: local bindings stay on this device; network work is a job."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid

from ..settings import atomic_json
from ..project_memory.store import MemoryStore
from ..project_memory_sync import MemoryRemote
from .. import project_memory_ssh


def register(app):
    root = app.data_dir / 'project-memory'
    store = MemoryStore(root)
    app.project_memory = store
    from ..project_memory.store import state_directory
    import sys
    client_path = state_directory() / 'client.json'
    client = {'python': sys.executable, 'core': str(Path(__file__).resolve().parents[2]), 'store': str(root.resolve())}
    # A second installation must not silently take over the active CLI store.
    if not client_path.exists():
        atomic_json(client_path, client)
    app.register('memory.client.configure', lambda p: atomic_json(client_path, client) or {'configured': True})
    config_path = app.data_dir / 'project-memory-local' / store.device_id / 'sync.json'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    ssh_path = config_path.parent / 'ssh.json'
    stop = threading.Event()
    state_lock = threading.RLock()
    lock = threading.Lock()

    def config():
        return json.loads(config_path.read_text('utf-8')) if config_path.exists() else {}

    def ssh_targets(_=None):
        with state_lock:
            value = json.loads(ssh_path.read_text('utf-8')) if ssh_path.exists() else {'targets': []}
            return {'targets': value.get('targets', [])}

    def ssh_configure(params):
        host, remote_root = project_memory_ssh.target(params)
        project_ids = params.get('project_ids', [])
        known = {p['id'] for p in store.projects()}
        if not isinstance(project_ids, list) or any(not isinstance(pid, str) or pid not in known for pid in project_ids):
            raise ValueError('请选择当前知识库中已有的项目')
        enabled = params.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('SSH 同步开关必须是布尔值')
        value = {'host': host, 'remote_root': remote_root, 'project_ids': sorted(set(project_ids)),
                 'enabled': enabled, 'library_id': store.info()['library_id']}
        if params.get('project_path'):
            _, project_path = project_memory_ssh.target({'host': host, 'remote_root': params['project_path']})
            if params.get('project_id') not in project_ids:
                raise ValueError('远端绑定的项目必须包含在同步选择中')
            value.update(project_path=project_path, project_id=params['project_id'])
        with state_lock:
            rows = ssh_targets()['targets']
            previous = next((row for row in rows if row['id'] == params.get('id')), None) if params.get('id') else next((row for row in rows if row['host'] == host and row['remote_root'] == remote_root), None)
            if params.get('id') and previous is None:
                raise ValueError('SSH 目标已不存在，请刷新后重试')
            value['id'] = previous['id'] if previous else str(uuid.uuid4())
            rows = [row for row in rows if row['id'] != value['id']] + [value]
            atomic_json(ssh_path, {'targets': rows})
        return {'target': value}

    def ssh_remove(params):
        with state_lock:
            rows = ssh_targets()['targets']
            atomic_json(ssh_path, {'targets': [row for row in rows if row['id'] != params['id']]})
        return {'ok': True}

    def remote():
        path = app.data_dir / 'webdav.json'
        value = json.loads(path.read_text('utf-8')) if path.exists() else {}
        if not value.get('url') or not value.get('username') or not value.get('password_dpapi'):
            raise ValueError('请先在设置中配置 WebDAV 地址、账号和密码')
        return MemoryRemote(value)

    def status(_):
        value = config()
        settings = app.data_dir / 'webdav.json'
        dav = json.loads(settings.read_text('utf-8')) if settings.exists() else {}
        webdav_configured = bool(dav.get('url') and dav.get('username') and dav.get('password_dpapi'))
        ssh_count = sum(bool(target.get('enabled')) for target in ssh_targets()['targets'])
        return {**store.info(), 'configured': webdav_configured or bool(ssh_count), 'webdav_configured': webdav_configured, 'ssh_target_count': ssh_count,
                'auto_sync': bool(value.get('auto_sync', False)), 'interval_seconds': value.get('interval_seconds', 300), 'last_result': value.get('last_result'), 'last_error': value.get('last_error'),
                'last_sync_at': value.get('last_sync_at')}

    def configure(params):
        with state_lock:
            value = config()
            if 'auto_sync' in params:
                if not isinstance(params['auto_sync'], bool):
                    raise ValueError('自动同步开关必须是布尔值')
                value['auto_sync'] = params['auto_sync']
            if 'interval_seconds' in params:
                value['interval_seconds'] = max(60, min(86400, int(params['interval_seconds'])))
            atomic_json(config_path, value)
        return status({})

    def sync_job(job):
        if not lock.acquire(blocking=False):
            raise ValueError('项目记忆同步正在运行')
        client = None
        try:
            warnings, exchanges = [], []
            targets = [target for target in ssh_targets()['targets'] if target.get('enabled')]
            for target in targets:
                job.check_cancelled()
                try:
                    if target.get('library_id') != store.info()['library_id']:
                        raise ValueError('目标绑定了另一知识库，请重新保存配置')
                    exchanges.append({'id': target['id'], **project_memory_ssh.sync(target, store, job)})
                except Exception as exc:
                    job.check_cancelled()
                    warnings.append({'id': target['id'], 'host': target['host'], 'message': str(exc)})
            if status({})['webdav_configured']:
                client = remote()
                result = client.sync(store, job, verify=bool(job.params.get('verify', False)))
            elif exchanges:
                result = {'uploaded': 0, 'downloaded': 0, 'library_id': store.info()['library_id'], 'webdav': 'not_configured'}
            elif warnings:
                raise ValueError('已配置的 SSH 目标均未同步成功：' + '；'.join(row['host'] + ': ' + row['message'] for row in warnings))
            else:
                raise ValueError('请先配置 WebDAV 或保存 SSH 同步目标')
            result.update(ssh=exchanges, warnings=warnings)
            with state_lock:
                atomic_json(config_path, {**config(), 'last_result': result, 'last_error': None, 'last_sync_at': datetime.now(timezone.utc).isoformat()})
            curate = getattr(app, 'memory_auto_curate', None)
            if curate:
                curate()
            return result
        except Exception as exc:
            with state_lock:
                atomic_json(config_path, {**config(), 'last_error': str(exc)})
            raise
        finally:
            if client:
                client.close()
            lock.release()

    def libraries_job(job):
        client = remote()
        try:
            result = {'libraries': client.libraries()}
            job.progress(100, '已读取服务器知识库')
            return result
        finally:
            client.close()

    def attachment(params):
        path = Path(params['path'])
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('请选择不超过 64 MB 的附件文件')
        return {**store.add_blob(path.read_bytes()), 'name': path.name}

    def attachment_path(params):
        content = store.get_blob(params['hash'])
        name = str(params.get('name') or 'attachment.bin')
        # Export a verified copy with a display filename, never interpret it as a path.
        if name != Path(name).name or '/' in name or '\\' in name or ':' in name or any(ord(c) < 32 for c in name) or name in ('.', '..'):
            raise ValueError('无效附件文件名')
        export = app.data_dir / 'project-memory-local' / store.device_id / 'attachments' / params['hash']
        export.mkdir(parents=True, exist_ok=True)
        target = export / name
        target.write_bytes(content)
        return {'path': str(target)}

    def bind(params):
        if params.get('kind', 'local') == 'local':
            path = Path(params['path']).expanduser().resolve()
            if not path.is_dir():
                raise ValueError('本机项目目录不存在；请先选择这台设备上已有的项目目录')
            result = store.bind_project(params['project_id'], str(path), kind='local')
            from ..project_memory.principles import initialize_project_rules
            initialize_project_rules(store, path)
            return result
        if params.get('kind') != 'ssh':
            raise ValueError('项目位置类型无效')
        project_memory_ssh.target({'host': params.get('host'), 'remote_root': params['path']})
        return store.bind_project(params['project_id'], params['path'], kind='ssh', host=params['host'])

    handlers = {
        'info': lambda p: store.info(),
        'projects': lambda p: {'projects': store.projects()},
        'project.create': lambda p: store.create_project(p['name'], p.get('id') or p.get('project_id')),
        'project.bind': bind,
        'project.unbind': lambda p: store.unbind_project(p['location_id']),
        'project.subscribe': lambda p: store.subscribe(p['project_id'], p.get('enabled', True)),
        'entries': lambda p: {'entries': store.entries(**{k: v for k, v in p.items() if k in ('project_id', 'kind', 'include_done', 'query', 'scope')})},
        'entry.get': lambda p: store.get_entry(p['id']),
        'entry.save': lambda p: store.save_entry(p['entry'], parents=p.get('parents')),
        'conflicts': lambda p: {'conflicts': store.conflicts()},
        'conflict.resolve': lambda p: store.resolve(p['id'], p['entry'], p['parents']),
        'attachment.add': attachment,
        'attachment.path': attachment_path,
        'ssh.targets': ssh_targets,
        'ssh.configure': ssh_configure,
        'ssh.remove': ssh_remove,
        'sync.status': status,
        'sync.configure': configure,
        'sync.join': lambda p: store.join_library(p['library_id']),
    }
    for name, handler in handlers.items():
        app.register('memory.' + name, handler)
    for name, runner in [('sync.run', sync_job), ('sync.libraries', libraries_job),
                         ('ssh.install', lambda job: project_memory_ssh.install(job.params, job)),
                         ('ssh.sync', lambda job: project_memory_ssh.sync(job.params, store, job))]:
        app.jobs.register('memory.' + name, runner)
        app.register('memory.' + name, lambda p, tool='memory.' + name: app.jobs.submit(tool, p))

    def auto_worker():
        import time
        last_attempt = 0.0
        while not stop.wait(2):
            try:
                value = status({})
                interval = value.get('interval_seconds', 300)
                if (not value['auto_sync'] or not value['configured'] or app.maintenance
                        or time.monotonic() - last_attempt < interval):
                    continue
                with app.data_lock:
                    if app.maintenance or stop.is_set():
                        continue
                    active = getattr(app.jobs, 'active', {})
                    if any(getattr(j, 'record', {}).get('tool') == 'memory.sync.run' for j in list(active.values())):
                        continue
                    app.jobs.submit('memory.sync.run', {})
                    last_attempt = time.monotonic()
            except Exception:
                # Job errors are visible in status and jobs. A failed configuration must
                # not crash the application or produce a busy retry loop.
                last_attempt = time.monotonic()

    thread = threading.Thread(target=auto_worker, name='project-memory-sync', daemon=True)
    thread.start()
    def stop_worker():
        stop.set()
        thread.join(timeout=3)
    def close():
        stop_worker()
        store.close()
    app.memory_stop = stop_worker
    app.memory_close = close
    app.memory_before_restore = store.close
    app.memory_after_restore = store.reopen
