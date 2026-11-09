"""Explicit configuration inventory; portable secrets only exist inside encrypted envelopes."""
import copy
import json

from .settings import protect

# Runtime receipts, caches, histories, database identity and migration retries are not settings.
FILES = {
    'quick-menu.json': set(), 'file-locks-menu.json': set(), 'relay-menu.json': set(),
    'orb-position.json': set(), 'ai-proxy-local.json': set(),
    'feishu-bridge.json': set(), 'qq-bridge.json': set(),
    'qq-bridge-credentials.json': {'credential_dpapi'},
    'fnconnect-login.json': {'secret'}, 'fnconnect/forwards.json': set(),
    'netmount.json': {'password_dpapi'},
    'stackchan.local.json': {'token_protected', 'printer_access_protected'},
    'netease/auth.dpapi.json': {'cookie'},
    'project-memory-ssh.json': set(),
}
TABLES = {'filesync_rules', 'ai_proxy_keys_v1'}


def path_for(app, name):
    if name == 'project-memory-ssh.json':
        device = app.project_memory.device_id if hasattr(app, 'project_memory') else 'unconfigured'
        return app.data_dir / 'project-memory-local' / device / 'ssh.json'
    return app.data_dir / name


def transform(value, fields, decrypt):
    if isinstance(value, dict):
        return {key: protect(item, decrypt=decrypt) if key in fields and item else transform(item, fields, decrypt)
                for key, item in value.items()}
    if isinstance(value, list):
        return [transform(item, fields, decrypt) for item in value]
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) != {'files', 'tables', 'ai_aggregation'}:
        raise ValueError('功能配置备份格式无效')
    if (not isinstance(value['files'], dict) or set(value['files']) - FILES.keys()
            or any(not isinstance(v, (dict, list)) for v in value['files'].values())
            or not isinstance(value['tables'], dict) or set(value['tables']) - TABLES
            or not isinstance(value['ai_aggregation'], dict)):
        raise ValueError('功能配置备份包含未登记的配置')
    for rows in value['tables'].values():
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows):
            raise ValueError('配置表记录无效')
        if len({r['id'] for r in rows}) != len(rows):
            raise ValueError('配置表包含重复标识')
    from .ai_aggregation_sync import validate as validate_account
    for identifier, row in value['ai_aggregation'].items():
        validate_account(row)
        if row['id'] != identifier or row['account'] is None:
            raise ValueError('AI 聚合备份账户标识无效')
    return copy.deepcopy(value)


def snapshot(app):
    files, tables = {}, {}
    for name, fields in FILES.items():
        path = path_for(app, name)
        if path.is_symlink():
            raise ValueError('配置文件不能是符号链接')
        if path.is_file():
            files[name] = transform(json.loads(path.read_text('utf-8')), fields, True)
    with app.storage.connect() as db:
        for table in TABLES:
            if db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
                rows = [json.loads(r[0]) for r in db.execute('SELECT record FROM '+table)]
                if table == 'filesync_rules':
                    rows = [{k: v for k, v in row.items() if k not in ('baseline', 'last', 'revision')} for row in rows]
                tables[table] = rows
    from .ai_aggregation_sync import local_accounts
    accounts = local_accounts(app) if hasattr(app, 'ai_proxy') else {}
    return validate(dict(files=files, tables=tables, ai_aggregation=accounts))


def local_revision(app):
    """Hash ciphertext and persisted configuration, never decrypt during status checks."""
    import hashlib
    digest = hashlib.sha256()
    for name in sorted(FILES):
        path = path_for(app, name)
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b'<absent>')
    with app.storage.connect() as db:
        for table in sorted(TABLES | {'ai_quota_accounts_v1', 'ai_call_routes_v1'}):
            digest.update(table.encode())
            if db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
                for row in db.execute('SELECT id,record FROM '+table+' ORDER BY id'):
                    # Quota refresh does not change user settings.
                    record = json.loads(row[1])
                    if table == 'ai_quota_accounts_v1':
                        record = {k:v for k,v in record.items() if k not in ('snapshot', 'checked_at', 'error', 'status')}
                    digest.update(json.dumps([row[0],record], sort_keys=True, ensure_ascii=False).encode())
    return digest.hexdigest()


def prepare(app, value):
    """Protect every imported credential before any disk write."""
    value = validate(value)
    return {path_for(app, name): transform(body, FILES[name], False) for name, body in value['files'].items()}


def table_backup(app):
    tables = TABLES | {'ai_quota_accounts_v1', 'ai_call_routes_v1'}
    with app.storage.connect() as db:
        return {table: list(db.execute('SELECT id,record FROM '+table)) for table in tables
                if db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()}


def restore_tables(app, value):
    with app.storage.connect() as db:
        for table, rows in value.items():
            db.execute('DELETE FROM '+table)
            db.executemany('INSERT INTO '+table+'(id,record) VALUES(?,?)', rows)


def apply(app, value):
    from .ai_aggregation_sync import apply_account
    value = validate(value)
    with app.storage.connect() as db:
        for table, rows in value['tables'].items():
            db.execute('CREATE TABLE IF NOT EXISTS '+table+'(id TEXT PRIMARY KEY,record TEXT NOT NULL)')
            db.execute('DELETE FROM '+table)
            for row in rows:
                row = dict(row)
                if table == 'filesync_rules':
                    # Cross-device paths require review before any copies or deletes.
                    row.update(auto=False, baseline={}, last=None)
                db.execute('INSERT INTO '+table+' VALUES(?,?)', (row['id'], json.dumps(row, ensure_ascii=False)))
    # Replace the chosen configuration snapshot, retaining a rollback copy in restore().
    if hasattr(app, 'ai_proxy'):
        with app.storage.connect() as db:
            db.execute('DELETE FROM ai_quota_accounts_v1')
            db.execute('DELETE FROM ai_call_routes_v1')
        for account in value['ai_aggregation'].values():
            apply_account(app, account)
