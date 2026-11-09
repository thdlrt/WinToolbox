"""Encrypted, causal AI aggregation configuration replication, separate from knowledge."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from pathlib import Path
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .settings import atomic_json, protect
from .project_memory.store import MemoryStore
from .project_memory_sync import MemoryRemote, canonical
from .webdav_layout import shared_config
from .features.ai_quota import BY_ID

FORMAT = 'wintoolbox-ai-aggregation-v1'
AAD = FORMAT.encode()


def encode(value):
    body = canonical(value)
    if len(body) > 1024 * 1024:
        raise ValueError('AI 聚合配置超过 1 MiB')
    return body


def seal(value, password):
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200000, 32)
    return dict(format=FORMAT, salt=base64.b64encode(salt).decode(), nonce=base64.b64encode(nonce).decode(),
                ciphertext=base64.b64encode(AESGCM(key).encrypt(nonce, encode(value), AAD)).decode())


def unseal(value, password):
    try:
        if set(value) != {'format', 'salt', 'nonce', 'ciphertext'} or value['format'] != FORMAT:
            raise ValueError()
        salt, nonce, cipher = [base64.b64decode(value[k], validate=True) for k in ('salt', 'nonce', 'ciphertext')]
        if len(salt) != 16 or len(nonce) != 12 or len(cipher) > 1024 * 1024 + 16:
            raise ValueError()
        key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 200000, 32)
        return validate(json.loads(AESGCM(key).decrypt(nonce, cipher, AAD)))
    except Exception:
        raise ValueError('AI 聚合配置解密或校验失败，请核对 WebDAV 密码；本机账户已保留') from None


def validate(value):
    if not isinstance(value, dict) or set(value) != {'id', 'account', 'route'} or not re.fullmatch('[a-f0-9]{32}', str(value['id'])):
        raise ValueError('AI 聚合账户标识无效')
    account, route = value['account'], value['route']
    if account is None:
        if route is not None:
            raise ValueError('已删除账户不能包含调用配置')
        return value
    if (not isinstance(account, dict) or set(account) != {'provider', 'name', 'created_at', 'credential'}
            or account['provider'] not in BY_ID or not isinstance(account['name'], str) or not 0 < len(account['name']) <= 80
            or type(account['created_at']) not in (int, float) or not math.isfinite(account['created_at']) or account['created_at'] < 0):
        raise ValueError('AI 聚合账户配置无效')
    for credential in [account['credential'], (route or {}).get('credential')]:
        if credential is not None and (not isinstance(credential, str) or len(credential) > 32768 or any(c in credential for c in '\r\n\x00')):
            raise ValueError('AI 聚合凭据格式无效')
    if route is not None and (not isinstance(route, dict) or set(route) != {'enabled', 'model', 'credential'}
                             or type(route['enabled']) is not bool or not isinstance(route['model'], str) or not 0 < len(route['model']) <= 160):
        raise ValueError('AI 聚合调用配置无效')
    encode(value)
    return value


def read_credential(value):
    try:
        return protect(value, decrypt=True)
    except Exception:
        raise ValueError('本机凭据无法解密，可能来自另一台设备；请编辑账户重新填写，或在 AI 聚合的配置同步中“使用远端覆盖”') from None


def components(app):
    calls = app.ai_proxy.calls
    return calls.quota, calls


def local_accounts(app):
    quota, calls = components(app)
    with app.settings.lock:
        result = {}
        for row in quota._all():
            account = {k: row[k] for k in ('provider', 'name', 'created_at')}
            account['credential'] = app.settings.secret(row['source_provider_id']) if row.get('source_provider_id') else read_credential(row['credential']) if row.get('credential') else None
            route = calls._get('ai_call_routes_v1', row['id'])
            if route is not None:
                credential = app.settings.secret(route['source_provider_id']) if route.get('source_provider_id') else read_credential(route['credential']) if route.get('credential') else None
                route = dict(enabled=route.get('enabled', True), model=route['model'], credential=credential)
            result[row['id']] = validate(dict(id=row['id'], account=account, route=route))
        return result


def local_revisions(app):
    quota, calls = components(app)
    return {row['id']: [row.get('revision'), (calls._get('ai_call_routes_v1', row['id']) or {}).get('revision')] for row in quota._all()}


def apply_account(app, value):
    value = validate(value)
    quota, calls = components(app)
    identifier, account, route = value['id'], value['account'], value['route']
    with quota.lock, calls.lock, app.storage.connect() as db:
        if account is None:
            db.execute('DELETE FROM ai_quota_accounts_v1 WHERE id=?', (identifier,))
            db.execute('DELETE FROM ai_call_routes_v1 WHERE id=?', (identifier,))
            return
        row = dict(id=identifier, **account, source_provider_id='', revision=uuid.uuid4().hex,
                   snapshot=None, status='not_checked', error=None, checked_at=None)
        row['credential'] = protect(account['credential']) if account['credential'] is not None else None
        db.execute('INSERT OR REPLACE INTO ai_quota_accounts_v1 VALUES(?,?)', (identifier, json.dumps(row, ensure_ascii=False)))
        if route is None:
            db.execute('DELETE FROM ai_call_routes_v1 WHERE id=?', (identifier,))
        else:
            record = dict(route, source_provider_id='')
            record['credential'] = protect(route['credential']) if route['credential'] is not None else None
            db.execute('INSERT OR REPLACE INTO ai_call_routes_v1 VALUES(?,?)', (identifier, json.dumps(record, ensure_ascii=False)))


class AggregationRemote(MemoryRemote):
    def __init__(self, config):
        super().__init__(config)
        self.base = self.directory + 'ai-aggregation/'


def publish(store, identifier, value, password, parents):
    # Preserve the versions actually seen by this device. A remote version pulled
    # during an in-flight local edit must remain a sibling, not be silently replaced.
    data = store._validate_data(dict(id=identifier, kind='knowledge', scope='global',
                                    title='AI 聚合账户配置', body='', internal=True,
                                    archived=True, sealed=seal(value, password)), 'entry')
    operation = dict(library_id=store.library_id, op_id=uuid.uuid4().hex, entity_id=identifier,
                     entity_type='entry', parents=parents, replica_id=store.replica_id,
                     device_id=store.device_id, scope='global', project_id=None, data=data,
                     created_at=datetime.now(timezone.utc).isoformat())
    store.ingest_operations([operation])
    return operation['op_id']


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def merge_values(base, left, right):
    if left == right:
        return left
    if left == base:
        return right
    if right == base:
        return left
    if all(isinstance(value, dict) for value in (base, left, right)) and set(base) == set(left) == set(right):
        return {key: merge_values(base[key], left[key], right[key]) for key in base}
    raise ValueError('同一配置字段存在不同修改')


def auto_merge(store, entry, password):
    if len(entry['versions']) != 2:
        return None
    operations = {op['op_id']: op for op in store.export_operations() if op['entity_id'] == entry['id']}
    def ancestors(identifier):
        seen, pending = set(), [identifier]
        while pending:
            current = pending.pop()
            if current not in seen and current in operations:
                seen.add(current)
                pending.extend(operations[current]['parents'])
        return seen
    left, right = entry['versions']
    common = ancestors(left['op_id']) & ancestors(right['op_id'])
    older = set().union(*(ancestors(identifier) - {identifier} for identifier in common)) if common else set()
    bases = common - older
    if len(bases) != 1:
        return None
    try:
        value = validate(merge_values(unseal(operations[bases.pop()]['data']['sealed'], password),
                                      unseal(left['data']['sealed'], password), unseal(right['data']['sealed'], password)))
    except ValueError:
        return None
    publish(store, entry['id'], value, password, entry['heads'])
    return store.get_entry(entry['id'])


def sync(app, job):
    config = shared_config(app.data_dir)
    password = protect(config['password_dpapi'], decrypt=True)
    endpoint = hashlib.sha256(canonical({k: config[k] for k in ('url', 'username', 'remote_path')})).hexdigest()
    root = app.data_dir / 'ai-aggregation-sync' / endpoint
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'baseline.json'
    baseline = json.loads(path.read_text('utf-8')) if path.exists() else {}
    store = MemoryStore(root, device_id=app.project_memory.device_id)
    remote = None
    try:
        store.join_library(FORMAT)
        captured = local_accounts(app)
        revisions = local_revisions(app)
        # Capture local edits before pulling. Published logs and hashes survive a retry.
        for identifier in sorted(set(captured) | {key for key in baseline if not key.startswith('@')}):
            job.check_cancelled()
            value = captured.get(identifier, dict(id=identifier, account=None, route=None))
            hashed = fingerprint(value)
            revision_key = '@revision:' + identifier
            if baseline.get(identifier) == hashed and baseline.get(revision_key) == revisions.get(identifier):
                continue
            try:
                previous = store.get_entry(identifier)
            except KeyError:
                previous = None
            if previous and previous['conflict']:
                # Only an explicit save after the conflict was observed resolves it.
                if baseline.get(revision_key) == revisions.get(identifier):
                    continue
            elif previous and unseal(previous['sealed'], password) == value:
                baseline[identifier] = hashed
                baseline[revision_key] = revisions.get(identifier)
                baseline['@heads:' + identifier] = previous['heads']
                continue
            parents = previous['heads'] if previous and baseline.get('@conflict:' + identifier) else baseline.get('@heads:' + identifier, [])
            operation_id = publish(store, identifier, value, password, parents)
            baseline['@heads:' + identifier] = [operation_id]
            baseline['@conflict:' + identifier] = False
            baseline[identifier] = hashed
            baseline[revision_key] = revisions.get(identifier)
        atomic_json(path, baseline)
        remote = AggregationRemote(config)
        result = remote.sync(store, job)
        changed, warnings = 0, []
        quota, calls = components(app)
        with app.data_lock, quota.lock, calls.lock:
            current = local_accounts(app)
            for entry in store.entries(include_done=True):
                if entry['conflict']:
                    merged = auto_merge(store, entry, password)
                    if merged is not None:
                        entry = merged
                        if hasattr(app, 'data_sync'):
                            app.data_sync.request('ai_aggregation', immediate=False)
                if entry['conflict']:
                    baseline['@conflict:' + entry['id']] = True
                    baseline['@revision:' + entry['id']] = local_revisions(app).get(entry['id'])
                    warnings.append({'message': 'AI 聚合账户存在并发修改，已保留双方版本；请在 AI 聚合页面选择本机或远端版本'})
                    continue
                value = unseal(entry['sealed'], password)
                if value['id'] != entry['id']:
                    raise ValueError('AI 聚合账户路径与内容不匹配')
                identifier = value['id']
                if current.get(identifier) != captured.get(identifier):
                    warnings.append({'message': '同步期间本机 AI 账户已修改，已保留，稍后继续同步'})
                    continue
                before = current.get(identifier, dict(id=identifier, account=None, route=None))
                if before != value:
                    job.check_cancelled()
                    apply_account(app, value)
                    changed += 1
                baseline[identifier] = fingerprint(value)
                baseline['@heads:' + identifier] = entry['heads']
                baseline['@conflict:' + identifier] = False
                baseline['@revision:' + identifier] = local_revisions(app).get(identifier)
            atomic_json(path, baseline)
        if changed:
            app.emit('ai.config.changed')
        return dict(result, applied=changed, warnings=warnings)
    finally:
        if remote:
            remote.close()
        store.close()


def register(app):
    from .data_sync import ensure_manager
    manager = ensure_manager(app)
    manager.register('ai_aggregation', 'AI 聚合配置', lambda: all(shared_config(app.data_dir).get(k) for k in ('url', 'username', 'password_dpapi')), lambda job: replace_remote(app, job) if getattr(job, 'params', {}).get('remote_only') else sync(app, job))
    app.jobs.register('ai_aggregation.use_remote', lambda job: manager.run('ai_aggregation', job))
    app.register('ai_aggregation.use_remote', lambda _: app.jobs.submit('ai_aggregation.use_remote', {'remote_only': True}))
    app.register('ai_aggregation.conflicts', lambda _: conflicts(app))
    app.register('ai_aggregation.resolve', lambda params: resolve_conflict(app, params))
    for method in ('ai_quota.save', 'ai_quota.remove', 'ai_calls.save', 'settings.update'):
        original = app.handlers[method]
        def changing(params, original=original):
            result = original(params)
            manager.request('ai_aggregation', immediate=False)
            return result
        app.handlers[method] = changing


def conflict_store(app):
    config = shared_config(app.data_dir)
    if not all(config.get(k) for k in ('url', 'username', 'remote_path', 'password_dpapi')):
        return None, None, None
    endpoint = hashlib.sha256(canonical({k: config[k] for k in ('url', 'username', 'remote_path')})).hexdigest()
    root = app.data_dir / 'ai-aggregation-sync' / endpoint
    if not (root / 'memory.sqlite3').is_file():
        return None, None, None
    return MemoryStore(root, device_id=app.project_memory.device_id), protect(config['password_dpapi'], decrypt=True), root


def conflicts(app):
    store, password, _ = conflict_store(app)
    if store is None:
        return {'items': []}
    try:
        items = []
        for entry in store.conflicts():
            versions = []
            for operation in entry['versions']:
                value = unseal(operation['data']['sealed'], password)
                account, route = value['account'], value['route']
                versions.append(dict(op_id=operation['op_id'], local=operation['device_id'] == store.device_id,
                                     name=account['name'] if account else '已删除账户', provider=account['provider'] if account else '',
                                     model=route['model'] if route else '', deleted=account is None, created_at=operation['created_at']))
            items.append(dict(id=entry['id'], parents=entry['heads'], versions=versions))
        return {'items': items}
    finally:
        store.close()


def resolve_conflict(app, params):
    manager = app.data_sync
    with app.data_lock:
        if manager.services['ai_aggregation']['syncing']:
            raise ValueError('AI 聚合正在同步，请在完成后重新采用版本')
        store, password, root = conflict_store(app)
        if store is None:
            raise ValueError('尚无冲突记录，请先同步并刷新')
        try:
            entry = store.get_entry(params.get('id'))
            if set(params.get('parents', [])) != set(entry['heads']):
                raise ValueError('冲突版本已变化，请刷新后重新选择')
            selected = next((op for op in entry['versions'] if op['op_id'] == params.get('op_id')), None)
            if selected is None:
                raise ValueError('所选版本已不存在，请刷新后重选')
            value = unseal(selected['data']['sealed'], password)
            quota, calls = components(app)
            with quota.lock, calls.lock:
                recovery = root / 'recovery' / (uuid.uuid4().hex + '.json')
                atomic_json(recovery, dict(id=entry['id'], account=quota._get(entry['id']), route=calls._get('ai_call_routes_v1', entry['id'])))
                operation_id = publish(store, entry['id'], value, password, entry['heads'])
                apply_account(app, value)
                baseline_path = root / 'baseline.json'
                baseline = json.loads(baseline_path.read_text('utf-8')) if baseline_path.exists() else {}
                baseline[entry['id']] = fingerprint(value)
                baseline['@revision:' + entry['id']] = local_revisions(app).get(entry['id'])
                baseline['@heads:' + entry['id']] = [operation_id]
                baseline['@conflict:' + entry['id']] = False
                atomic_json(baseline_path, baseline)
            manager.request('ai_aggregation')
            app.emit('ai.config.changed')
            return dict(resolved=True, recovery_path=str(recovery))
        finally:
            store.close()


def replace_remote(app, job):
    """A recovery path which never has to decrypt the old device's local credentials."""
    config = shared_config(app.data_dir)
    password = protect(config['password_dpapi'], decrypt=True)
    endpoint = hashlib.sha256(canonical({k: config[k] for k in ('url', 'username', 'remote_path')})).hexdigest()
    root = app.data_dir / 'ai-aggregation-sync' / endpoint
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='remote-', dir=root) as directory:
        staged = MemoryStore(Path(directory), device_id=app.project_memory.device_id)
        remote = AggregationRemote(config)
        try:
            staged.join_library(FORMAT)
            try:
                remote.sync(staged, job, read_only=True)
            except ValueError as exc:
                if '所选远端知识库已不存在' in str(exc):
                    raise ValueError('远端尚无 AI 聚合配置，请先在原设备同步后重试；本机配置未改') from None
                raise
            if not staged.info()['operation_count']:
                raise ValueError('远端尚无 AI 聚合配置，请先在原设备同步；本机配置保持完整')
            if staged.conflicts():
                raise ValueError('远端 AI 聚合配置仍有冲突，请先在已同步设备选择版本，再使用远端覆盖')
            operations = staged.export_operations()
            known = {op['op_id'] for op in operations}
            if any(parent not in known for op in operations for parent in op['parents']):
                raise ValueError('远端配置缺少历史版本，请在原设备完成同步后重试；本机配置未改')
            entries = staged.entries(include_done=True)
            values = [unseal(entry['sealed'], password) for entry in entries]
            if any(value['id'] != entry['id'] for value, entry in zip(values, entries)):
                raise ValueError('远端配置标识不匹配，请选择其他备份或在原设备重新同步')
            quota, calls = components(app)
            with app.data_lock, quota.lock, calls.lock:
                backup = root / 'recovery' / uuid.uuid4().hex
                backup.mkdir(parents=True)
                with app.storage.connect() as db:
                    before = {table: list(db.execute('SELECT id,record FROM '+table)) for table in ('ai_quota_accounts_v1', 'ai_call_routes_v1')}
                atomic_json(backup / 'accounts.json', before)
                baseline_path = root / 'baseline.json'
                baseline_before = baseline_path.read_bytes() if baseline_path.exists() else None
                if baseline_before is not None:
                    (backup / 'baseline.json').write_bytes(baseline_before)
                store = MemoryStore(root, device_id=app.project_memory.device_id)
                try:
                    store.join_library(FORMAT)
                    with sqlite3.connect(backup / 'memory.sqlite3') as db:
                        store.db.backup(db)
                    job.check_cancelled()
                    try:
                        with app.storage.connect() as db:
                            db.execute('DELETE FROM ai_quota_accounts_v1')
                            db.execute('DELETE FROM ai_call_routes_v1')
                        for value in values:
                            apply_account(app, value)
                        with store.db:
                            store.db.execute('DELETE FROM operations')
                            store.ingest_operations(operations)
                        revisions = local_revisions(app)
                        baseline = {}
                        for entry, value in zip(entries, values):
                            identifier = value['id']
                            baseline[identifier] = fingerprint(value)
                            baseline['@heads:' + identifier] = entry['heads']
                            baseline['@revision:' + identifier] = revisions.get(identifier)
                            baseline['@conflict:' + identifier] = False
                        atomic_json(baseline_path, baseline)
                    except BaseException:
                        with app.storage.connect() as db:
                            for table, rows in before.items():
                                db.execute('DELETE FROM '+table)
                                db.executemany('INSERT INTO '+table+' VALUES(?,?)', rows)
                        # Restore the local operation log, including any unsent edits.
                        with sqlite3.connect(backup / 'memory.sqlite3') as original:
                            original.backup(store.db)
                        if baseline_before is None:
                            baseline_path.unlink(missing_ok=True)
                        else:
                            baseline_path.write_bytes(baseline_before)
                        raise
                    if hasattr(job, 'mark_committed'):
                        job.mark_committed()
                finally:
                    store.close()
                app.emit('ai.config.changed')
                return dict(applied=len(values), mode='remote', recovery_path=str(backup), warnings=[])
        finally:
            remote.close()
            staged.close()
