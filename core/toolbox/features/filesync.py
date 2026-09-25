"""Content-based local sync with durable baselines and reviewed, guarded writes."""
import copy
import contextlib
import fnmatch
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import uuid

from ._common import write_json


def safe_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('请使用绝对路径')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError(f'不支持符号链接或目录联接：{part}')
    return path.resolve()


def signature(path, check=lambda: None):
    path = safe_path(path)
    try:
        before = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f'不是普通文件：{path}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            check()
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f'扫描期间文件发生变化，请重试：{path}')
    return {'hash': digest.hexdigest(), 'size': after.st_size, 'mtime': after.st_mtime_ns}


def equal(a, b):
    return a is None and b is None or bool(a and b and a['hash'] == b['hash'])


def matches(name, patterns):
    name = name.casefold()
    return any(fnmatch.fnmatchcase(name, p.casefold()) or
               p.startswith('**/') and fnmatch.fnmatchcase(name, p[3:].casefold()) for p in patterns)


def included(name, rule):
    parts = Path(name).parts
    if any(p.casefold() in ('.back', '.git') for p in parts) or name.endswith('.filesync.tmp'):
        return False
    return (not rule['include'] or matches(name, rule['include'])) and not matches(name, rule['exclude'])


def validate(value):
    kind = value.get('kind', 'file')
    if kind not in ('file', 'folder'):
        raise ValueError('同步类型必须为文件或文件夹')
    source, target = (safe_path(str(value.get(k, ''))) for k in ('source', 'target'))
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError('源与目标不能相同或互相包含')
    if source.exists() and target.exists() and source.samefile(target):
        raise ValueError('源与目标指向同一个文件')
    for path in (source, target):
        if path.exists() and (path.is_dir() != (kind == 'folder')):
            raise ValueError(f'路径类型与规则不符：{path}')
    interval = int(value.get('interval', 30))
    retention = int(value.get('retention', 30))
    if not 5 <= interval <= 86400 or not 0 <= retention <= 3650:
        raise ValueError('检查间隔应为 5–86400 秒，备份保留应为 0–3650 天（0 为永久）')
    rule = {'name': str(value.get('name', '')).strip() or source.name, 'kind': kind,
            'source': str(source), 'target': str(target), 'interval': interval, 'retention': retention}
    for key in ('bidirectional', 'auto', 'deletes'):
        if not isinstance(value.get(key, False), bool):
            raise ValueError(f'{key} 必须为布尔值')
        rule[key] = value.get(key, False)
    for key in ('include', 'exclude'):
        patterns = value.get(key, [])
        if not isinstance(patterns, list) or any(not isinstance(p, str) or len(p) > 500 for p in patterns):
            raise ValueError('过滤规则应为 glob 字符串列表')
        rule[key] = [p.strip().replace('\\', '/') for p in patterns if p.strip()]
    return rule


def scan(rule, side, check):
    root = safe_path(rule[side])
    if rule['kind'] == 'file':
        value = signature(root, check)
        return {'': value} if value else {}
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ValueError(f'目录不可用：{root}')
    result = {}
    def fail(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        check()
        dirs[:] = [d for d in dirs if d.casefold() not in ('.back', '.git')]
        for name in dirs:
            safe_path(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if included(relative, rule):
                value = signature(path, check)
                if value is None:
                    raise ValueError(f'扫描期间文件消失，请重试：{path}')
                result[relative] = value
    return result


def build_plan(rule, baseline, check=lambda: None):
    validate(rule)
    source, target = Path(rule['source']), Path(rule['target'])
    if rule['kind'] == 'folder' and baseline and (not source.is_dir() or not target.is_dir()):
        raise ValueError('已同步的根目录不可用；请恢复目录后重试，不将离线目录视为删除')
    if rule['kind'] == 'file' and baseline and (not source.parent.is_dir() or not target.parent.is_dir()):
        raise ValueError('文件所在目录不可用，不将离线目录视为删除')
    if not rule['bidirectional'] and not source.exists():
        raise ValueError('源路径不存在，请恢复后重试')
    if not source.exists() and not target.exists() and not baseline:
        raise ValueError('两侧路径均不存在')
    left, right = scan(rule, 'source', check), scan(rule, 'target', check)
    operations = []
    for name in sorted(left.keys() | right.keys() | baseline.keys()):
        check()
        if rule['kind'] == 'folder' and not included(name, rule):
            continue
        a, b, old = left.get(name), right.get(name), baseline.get(name)
        action, reason = 'skip', '内容相同'
        if not equal(a, b):
            if not rule['bidirectional']:
                if a:
                    action, reason = 'to_target', '源文件新增或内容变化'
                elif b and rule['deletes']:
                    action, reason = 'delete_target', '源端已删除，先备份目标'
                else:
                    reason = '保留目标独有文件'
            elif old is None:
                if a and b:
                    action, reason = 'conflict', '首次同步两侧内容不同，请明确选择保留哪一侧'
                else:
                    action, reason = ('to_target' if a else 'to_source'), '补齐新增文件'
            else:
                changed_a, changed_b = not equal(a, old), not equal(b, old)
                if changed_a and changed_b:
                    action, reason = 'conflict', '两侧同时修改，或一侧删除而另一侧修改'
                elif not a or not b:
                    if rule['deletes']:
                        action, reason = ('delete_target' if b else 'delete_source'), '传播已同步文件的删除，先备份'
                    else:
                        action, reason = ('to_source' if b else 'to_target'), '不传播删除，恢复缺失副本'
                else:
                    action, reason = ('to_target' if changed_a else 'to_source'), '仅一侧内容变化'
        operations.append({'path': name, 'action': action, 'reason': reason})
    payload = {'rule': rule, 'left': left, 'right': right, 'operations': operations}
    payload['token'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


def file_at(rule, side, name):
    root = safe_path(rule[side])
    if rule['kind'] == 'file':
        return root
    result = safe_path(root / name)
    if not result.is_relative_to(root):
        raise ValueError('文件路径越界')
    return result


def backup_root(rule, side):
    return safe_path(Path(rule[side]).parent / '.back' / 'wintoolbox-filesync' / rule['id'])


def backup(rule, side, path, expected, check):
    root = backup_root(rule, side) / f'{time.time_ns()}-{uuid.uuid4().hex[:8]}'
    root.mkdir(parents=True, exist_ok=False)
    destination = root / 'content'
    shutil.copy2(path, destination)
    if not equal(signature(destination, check), expected) or signature(path, check) != expected:
        raise ValueError(f'备份期间文件变化，未覆盖原文件：{path}')
    write_json(root / 'manifest.json', {'format': 'wintoolbox-filesync-backup-v1', 'rule_id': rule['id'],
                                      'original': str(path), 'created': time.time(), 'hash': expected['hash']})
    return str(root)


def purge_backups(rule, check):
    if not rule['retention']:
        return
    cutoff = time.time() - rule['retention'] * 86400
    for root in {backup_root(rule, side) for side in ('source', 'target')}:
        if not root.exists():
            continue
        for entry in root.iterdir():
            check()
            safe_path(entry)
            manifest = safe_path(entry / 'manifest.json')
            content = safe_path(entry / 'content')
            if not manifest.is_file() or not content.is_file():
                continue
            value = json.loads(manifest.read_text('utf-8'))
            if (value.get('format') != 'wintoolbox-filesync-backup-v1' or value.get('rule_id') != rule['id']
                    or value.get('created', time.time()) >= cutoff):
                continue
            # Only our exact two-file backup format can be removed. Never recurse through a shared .back.
            if {p.name for p in entry.iterdir()} == {'manifest.json', 'content'}:
                content.unlink()
                manifest.unlink()
                entry.rmdir()


def guarded_copy(source, target, expected_source, expected_target, check):
    safe_path(source)
    safe_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.wintoolbox-', suffix='.filesync.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as dest, source.open('rb') as src:
            while block := src.read(1024 * 1024):
                check()
                dest.write(block)
            dest.flush()
            os.fsync(dest.fileno())
        if not equal(signature(Path(temporary), check), expected_source):
            raise ValueError('复制期间源内容变化，未替换目标')
        if signature(source, check) != expected_source or signature(target, check) != expected_target:
            raise ValueError('文件在预览后发生变化，请重新预览')
        check()
        safe_path(target)
        os.utime(temporary, ns=(expected_source['mtime'], expected_source['mtime']))
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class FileSync:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.execution = threading.Lock()
        self.stop_event = threading.Event()
        self.due = {}
        self.pending = {}
        self.init_tables()

    def init_tables(self):
        with self.app.storage.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS filesync_rules(id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS filesync_history(id TEXT PRIMARY KEY, created REAL NOT NULL, record TEXT NOT NULL);''')

    def rules(self):
        with self.app.storage.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM filesync_rules ORDER BY rowid')]

    def get(self, identifier):
        for rule in self.rules():
            if rule['id'] == identifier:
                return rule
        raise ValueError('同步规则不存在')

    def put(self, rule):
        with self.app.storage.connect() as db:
            db.execute('INSERT OR REPLACE INTO filesync_rules VALUES(?,?)', (rule['id'], json.dumps(rule, ensure_ascii=False)))

    def save(self, params):
        with self.edit_gate(), self.lock:
            rule = validate(params)
            old = self.get(params['id']) if params.get('id') else None
            for other in self.rules():
                if old and old['id'] == other['id']:
                    continue
                for a in (rule['source'], rule['target']):
                    for b in (other['source'], other['target']):
                        if Path(a) == Path(b) or Path(a).is_relative_to(b) or Path(b).is_relative_to(a):
                            raise ValueError('同步路径与另一条规则重叠，请先调整或删除原规则')
            identity = ('source', 'target', 'kind', 'bidirectional', 'include', 'exclude')
            baseline = old.get('baseline', {}) if old and all(old[k] == rule[k] for k in identity) else {}
            rule.update(id=old['id'] if old else uuid.uuid4().hex, baseline=baseline,
                        revision=uuid.uuid4().hex, last=old.get('last') if old else None)
            self.put(rule)
            return self.public(rule)

    @contextlib.contextmanager
    def edit_gate(self):
        if not self.execution.acquire(blocking=False):
            raise ValueError('同步或预览正在进行，请等待完成后编辑规则')
        try:
            yield
        finally:
            self.execution.release()

    @staticmethod
    def public(rule):
        return {k: v for k, v in rule.items() if k != 'baseline'}

    def status(self, _=None):
        with self.app.storage.connect() as db:
            history = [json.loads(row[0]) for row in db.execute('SELECT record FROM filesync_history ORDER BY created DESC LIMIT 60')]
        candidates = [Path(os.environ.get(key, '~')).expanduser() / 'com.filesync.notes' / 'store.json'
                      for key in ('APPDATA', 'LOCALAPPDATA')]
        return {'rules': [self.public(r) for r in self.rules()], 'history': history,
                'legacy_paths': [str(p) for p in candidates if p.is_file()],
                'legacy_logs': [str(p.with_name('app.log.jsonl')) for p in candidates if p.with_name('app.log.jsonl').is_file()]}

    def remove(self, params):
        with self.edit_gate(), self.lock, self.app.storage.connect() as db:
            db.execute('DELETE FROM filesync_rules WHERE id=?', (params['id'],))
        return {'ok': True}

    def history(self, record):
        with self.app.storage.connect() as db:
            db.execute('INSERT INTO filesync_history VALUES(?,?,?)', (uuid.uuid4().hex, time.time(), json.dumps(record, ensure_ascii=False)))
            db.execute('DELETE FROM filesync_history WHERE id NOT IN (SELECT id FROM filesync_history ORDER BY created DESC LIMIT 300)')

    def run(self, job, preview=False):
        # One engine-wide gate also prevents manual and automatic jobs overlapping.
        while not self.execution.acquire(timeout=.2):
            job.check_cancelled()
        rule = None
        count, backups = 0, []
        try:
            rule = self.get(job.params['id'])
            if job.params.get('automatic') and (not rule['auto'] or self.stop_event.is_set()):
                return {'skipped': True}
            job.progress(0, '正在校验文件内容')
            plan = build_plan(rule, rule['baseline'], job.check_cancelled)
            if preview:
                return {'id': rule['id'], 'token': plan['token'], 'operations': plan['operations'],
                        'changes': sum(o['action'] != 'skip' for o in plan['operations'])}
            if not job.params.get('automatic') and job.params.get('token') != plan['token']:
                raise ValueError('规则或文件已变化，请重新预览后执行')
            resolutions = job.params.get('resolutions', {})
            if not isinstance(resolutions, dict):
                raise ValueError('冲突选择格式无效')
            operations = copy.deepcopy(plan['operations'])
            for operation in operations:
                if operation['action'] == 'conflict':
                    choice = resolutions.get(operation['path'])
                    if choice not in ('source', 'target'):
                        raise ValueError('存在双向冲突，请在预览中选择保留源端或目标端')
                    selected = plan['left' if choice == 'source' else 'right'].get(operation['path'])
                    other = 'target' if choice == 'source' else 'source'
                    operation['action'] = ('to_' if selected else 'delete_') + other
            baseline = copy.deepcopy(rule['baseline'])
            for index, operation in enumerate(operations):
                job.check_cancelled()
                name, action = operation['path'], operation['action']
                a, b = plan['left'].get(name), plan['right'].get(name)
                if action != 'skip':
                    side = 'target' if action.endswith('target') else 'source'
                    opposite = 'source' if side == 'target' else 'target'
                    dest, src = file_at(rule, side, name), file_at(rule, opposite, name)
                    expected, origin = (b, a) if side == 'target' else (a, b)
                    if signature(dest, job.check_cancelled) != expected or signature(src, job.check_cancelled) != origin:
                        raise ValueError('文件在扫描后变化，请重新预览')
                    if expected:
                        backups.append(backup(rule, side, dest, expected, job.check_cancelled))
                    if action.startswith('delete_'):
                        job.check_cancelled()
                        if signature(dest, job.check_cancelled) != expected or signature(src, job.check_cancelled) != origin:
                            raise ValueError('删除前文件变化，已保留原文件')
                        dest.unlink()
                        baseline.pop(name, None)
                    else:
                        guarded_copy(src, dest, origin, expected, job.check_cancelled)
                        baseline[name] = origin
                    count += 1
                elif equal(a, b):
                    if a:
                        baseline[name] = a
                    else:
                        baseline.pop(name, None)
                # Persist per-file progress so interruption cannot turn completed deletes into additions.
                rule['baseline'] = baseline
                self.put(rule)
                job.progress((index + 1) / max(1, len(operations)) * 95, f'已处理 {index + 1}/{len(operations)} 项')
            result = {'id': rule['id'], 'name': rule['name'], 'time': time.time(), 'ok': True,
                      'changed': count, 'backups': backups, 'message': f'同步完成，变更 {count} 项'}
            try:
                purge_backups(rule, job.check_cancelled)
            except (OSError, ValueError, TypeError) as exc:
                result['message'] += f'；旧备份清理未完成：{exc}'
            rule['last'] = result
            self.put(rule)
            self.history(result)
            job.mark_committed()
            return result
        except Exception as exc:
            if rule and not preview:
                result = {'id': rule['id'], 'name': rule['name'], 'time': time.time(), 'ok': False,
                          'changed': count, 'backups': backups, 'message': str(exc)}
                rule['last'] = result
                self.put(rule)
                self.history(result)
            raise
        finally:
            self.execution.release()

    def import_legacy(self, job):
        path = Path(job.params['path'])
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('旧配置超过 10 MB')
        recovered = path.suffix.lower() == '.jsonl'
        if recovered:
            # Logs preserve paths, not direction/filters. Recover into a conservative manual rule.
            found = {}
            for line in path.read_text('utf-8-sig').splitlines():
                job.check_cancelled()
                try:
                    message = json.loads(line).get('message', '')
                except (ValueError, AttributeError):
                    continue
                match = re.search(r'开始执行规则“(.+?)”，触发方式：.*?，源：(.+?)，目标：(.+)$', message)
                if not match:
                    continue
                name, source, target = match.groups()
                key = (os.path.normcase(source), os.path.normcase(target))
                existing = next((p for p in (Path(source), Path(target)) if p.exists()), None)
                if existing is not None:
                    found[key] = {'name': name, 'sourcePath': source, 'targetPath': target,
                                  'kind': 'folder' if existing.is_dir() else 'file', 'bidirectional': True}
            if not found:
                raise ValueError('日志中没有可恢复且至少一侧仍存在的同步路径')
            data = {'rules': list(found.values())}
        else:
            try:
                data = json.loads(path.read_text('utf-8-sig'))
            except ValueError as exc:
                raise ValueError('旧配置已损坏，可选择同目录 app.log.jsonl 恢复路径') from exc
        if not isinstance(data, dict) or not isinstance(data.get('rules'), list):
            raise ValueError('不是 FileSync store.json 配置')
        imported, errors = [], []
        for item in data['rules']:
            job.check_cancelled()
            try:
                rule = self.save({'name': item.get('name', ''), 'kind': item.get('kind', 'file'),
                    'source': item['sourcePath'], 'target': item['targetPath'],
                    'bidirectional': item.get('bidirectional', False), 'auto': False,
                    'deletes': item.get('deletePolicy') == 'moveToBackup',
                    'interval': max(5, min(86400, int(item.get('pollIntervalSec', 30)))),
                    'retention': data.get('settings', {}).get('backupRetentionDays', 30),
                    'include': item.get('includeGlobs', []), 'exclude': item.get('excludeGlobs', [])})
                imported.append(rule)
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                errors.append({'name': item.get('name', '') if isinstance(item, dict) else '', 'message': str(exc)})
        return {'imported': imported, 'errors': errors, 'recovered': recovered}

    def tick(self):
        if self.app.maintenance or self.stop_event.is_set():
            return
        with self.app.data_lock:
            if self.app.maintenance or self.stop_event.is_set():
                return
            for rule in self.rules():
                identifier = rule['id']
                if identifier in self.pending:
                    if self.app.jobs.get(self.pending[identifier])['status'] in ('queued', 'running', 'cancelling'):
                        continue
                    self.pending.pop(identifier)
                if rule['auto'] and time.monotonic() >= self.due.get(identifier, 0):
                    self.due[identifier] = time.monotonic() + rule['interval']
                    record = self.app.jobs.submit('filesync.sync', {'id': identifier, 'automatic': True})
                    self.pending[identifier] = record['id']

    def start(self):
        def loop():
            while not self.stop_event.wait(2):
                try:
                    self.tick()
                except Exception:
                    import logging
                    logging.exception('FileSync automatic check failed')
        self.thread = threading.Thread(target=loop, name='filesync-poll', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()


def register(app):
    service = FileSync(app)
    app.filesync = service
    app.register('filesync.status', service.status)
    app.register('filesync.save', service.save)
    app.register('filesync.remove', service.remove)
    for method, runner in [('preview', lambda job: service.run(job, preview=True)),
                           ('sync', service.run), ('import', service.import_legacy)]:
        name = 'filesync.' + method
        app.jobs.register(name, runner)
        app.register(name, lambda p, name=name: app.jobs.submit(name, p))
    service.start()
