"""Explicit, backed-up knowledge library merge or remote replacement."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import uuid

from .settings import atomic_json
from .project_memory.store import MemoryStore
from .project_memory_sync import blob_hashes


def reconcile(store, remote, library_id, mode, job, data_lock):
    if mode not in ('merge', 'remote') or not isinstance(library_id, str) or not library_id.strip():
        raise ValueError('请选择知识库及合并方式')
    with tempfile.TemporaryDirectory(prefix='library-connect-', dir=store.root.parent) as directory:
        staged = MemoryStore(Path(directory), device_id=store.device_id)
        try:
            staged.join_library(library_id)
            job.progress(5, '正在读取并校验远端知识库，本机数据尚未修改')
            remote.sync(staged, job, read_only=True)
            for project in staged.projects():
                staged.subscribe(project['id'])
            remote.sync(staged, job, read_only=True)
            incoming = staged.export_operations()
            known = {op['op_id'] for op in incoming}
            if any(parent not in known for op in incoming for parent in op['parents']):
                raise ValueError('远端知识库缺少父版本；请先在原设备完成同步后重试，本机数据保持完整')
            # Hold the local write gate only for merging/copying durable local state.
            with data_lock, store.lock:
                previous_id = store.library_id
                local = store.export_operations()
                if mode == 'merge':
                    merged = copy.deepcopy(local)
                    for op in merged:
                        op['library_id'] = library_id
                    for digest in blob_hashes(merged):
                        staged.add_blob(store.get_blob(digest))
                    try:
                        staged.ingest_operations(merged)
                    except ValueError as exc:
                        raise ValueError('两库含有不兼容的同 ID 操作；可选择“使用远端覆盖”，本机将先备份。详细原因：' + str(exc)) from None
                operations = staged.export_operations()
                for digest in blob_hashes(operations):
                    store.add_blob(staged.get_blob(digest))
                projects = {p['id'] for p in staged.projects()}
                locations = list(store.db.execute('SELECT id,device_id,project_id,payload FROM locations'))
                subscriptions = list(staged.db.execute('SELECT device_id,project_id,enabled FROM subscriptions'))
                if mode == 'merge':
                    prior_subscriptions = {(r[0], r[1]): r[2] for r in store.db.execute('SELECT device_id,project_id,enabled FROM subscriptions')}
                    subscriptions = [(r[0], r[1], prior_subscriptions.get((r[0], r[1]), r[2])) for r in subscriptions]
                warnings = []
                backup = store.root.parent / 'project-memory-local' / 'library-recovery' / uuid.uuid4().hex
                backup.mkdir(parents=True)
                with sqlite3.connect(backup / 'memory.sqlite3') as target:
                    store.db.backup(target)
                missing_blobs = []
                for digest in blob_hashes(local):
                    try:
                        body = store.get_blob(digest)
                        (backup / 'blobs').mkdir(exist_ok=True)
                        (backup / 'blobs' / digest).write_bytes(body)
                    except (OSError, ValueError):
                        missing_blobs.append(digest)
                if missing_blobs:
                    warnings.append({'message': '本机原库已有缺失或损坏附件；操作日志已备份，缺失列表见恢复说明'})
                atomic_json(backup / 'recovery.json', dict(previous_library_id=previous_id, target_library_id=library_id,
                            mode=mode, created_at=datetime.now(timezone.utc).isoformat(), local_operations=len(local), missing_blobs=missing_blobs))
                job.check_cancelled()
                with store.db:
                    store.db.execute('DELETE FROM operations')
                    store.db.executemany('INSERT INTO operations VALUES(?,?,?,?)',
                        [(op['op_id'], op['entity_id'], op['entity_type'], json.dumps(op, ensure_ascii=False, sort_keys=True, separators=(',', ':'))) for op in operations])
                    store.db.execute('UPDATE metadata SET value=? WHERE key=?', (library_id, 'library_id'))
                    store.db.execute('DELETE FROM subscriptions')
                    store.db.executemany('INSERT INTO subscriptions VALUES(?,?,?)', subscriptions)
                    for location in locations:
                        if location[2] not in projects:
                            store.db.execute('DELETE FROM locations WHERE id=?', (location[0],))
                store.library_id = library_id
                if hasattr(job, 'mark_committed'):
                    job.mark_committed()
                for location in locations:
                    value = json.loads(location[3])
                    if value['device_id'] != store.device_id or value['kind'] != 'local' or value['project_id'] not in projects:
                        continue
                    for context in ('.agent', '.agents'):
                        manifest = Path(value['path']) / context / 'toolbox.json'
                        if not manifest.is_file():
                            continue
                        try:
                            binding = json.loads(manifest.read_text('utf-8'))
                            if binding.get('library_id') == previous_id and binding.get('project_id') == value['project_id']:
                                (backup / (location[0] + '-' + context[1:] + '.json')).write_bytes(manifest.read_bytes())
                                atomic_json(manifest, dict(binding, library_id=library_id))
                        except (OSError, ValueError) as exc:
                            warnings.append({'message': '本机目录关联未更新，请在项目中重新关联：' + str(manifest) + '；' + str(exc)})
                ssh_path = store.root.parent / 'project-memory-local' / store.device_id / 'ssh.json'
                if ssh_path.is_file():
                    try:
                        ssh = json.loads(ssh_path.read_text('utf-8'))
                        (backup / 'ssh.json').write_bytes(ssh_path.read_bytes())
                        for target in ssh.get('targets', []):
                            if target.get('library_id') == previous_id:
                                target['library_id'] = library_id
                                if any(pid not in projects for pid in target.get('project_ids', [])):
                                    target['enabled'] = False
                                    warnings.append({'message': '部分 SSH 目标的项目不在远端库中，已暂停；请重新选择项目后启用'})
                        atomic_json(ssh_path, ssh)
                    except (OSError, ValueError) as exc:
                        warnings.append({'message': '请重新保存 SSH 同步目标：' + str(exc)})
                store.on_change()
                return dict(mode=mode, library_id=library_id, recovery_path=str(backup),
                            operation_count=len(operations), conflicts=len(store.conflicts()), warnings=warnings)
        finally:
            staged.close()
