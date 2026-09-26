"""Monthly expenses with exact money and owned, portable receipt attachments."""
import contextlib
import copy
import datetime
import hashlib
import json
import re
import tempfile
import threading
import time
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path, PurePosixPath

from ..jobs import Cancelled
from ..settings import atomic_json
from ..ledger import Ledger, DEFAULT_PROJECT
from ..ledger_service import register_service


CURRENCIES = ('CNY', 'USD', 'EUR', 'HKD')
STATUSES = ('waiting', 'reimbursed', 'non_reimbursable')
ENTRY_TYPES = ('expense', 'income')
LEGACY_STATUSES = {'unsubmitted': 'waiting', 'submitted': 'waiting', 'personal': 'non_reimbursable'}
KINDS = ('invoice', 'receipt', 'payment', 'other')
TYPES = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.webp': 'image/webp', '.ofd': 'application/ofd'}
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 200 * 1024 * 1024


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('无效的费用或附件记录')
    return value


def money(value):
    if (not isinstance(value, str) or not re.fullmatch(r'\d{1,12}(?:\.\d{1,2})?', value.strip(), re.ASCII)):
        raise ValueError('金额请填写非负数字，最多两位小数')
    amount = Decimal(value.strip())
    if amount > Decimal('999999999.99'):
        raise ValueError('金额不能超过 999999999.99')
    return format(amount.quantize(Decimal('.01')), 'f'), int(amount * 100)


def formatted(minor):
    absolute = abs(minor)
    return ('-' if minor < 0 else '') + f'{absolute // 100}.{absolute % 100:02d}'


def normalized_status(value, entry_type):
    if entry_type == 'income':
        return 'not_applicable'
    if not isinstance(value, str):
        raise ValueError('请选择有效的报销状态')
    value = LEGACY_STATUSES.get(value, value)
    if value not in STATUSES:
        raise ValueError('请选择已报销、等待报销或不可报销')
    return value


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value, re.ASCII):
        raise ValueError('日期请使用 YYYY-MM-DD 格式')
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError('日期无效') from None


def month_value(value):
    if value is None:
        return datetime.date.today().isoformat()[:7]
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value, re.ASCII):
        raise ValueError('月份请使用 YYYY-MM 格式')
    iso_date(value + '-01')
    return value


def period(params):
    has_range = 'start_month' in params or 'end_month' in params
    if not has_range:
        month = month_value(params.get('month'))
        return month, month
    if 'month' in params:
        raise ValueError('请选择单月或起止月份，不要同时填写')
    if not params.get('start_month') or not params.get('end_month'):
        raise ValueError('开始月份和结束月份必须同时填写')
    start, end = month_value(params['start_month']), month_value(params['end_month'])
    if start > end:
        raise ValueError('开始月份不能晚于结束月份')
    return start, end


def no_links(path, stop=None):
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('不支持符号链接或目录联接，请选择原始文件')
        if stop is not None and part == stop:
            break


def verify_type(path, suffix):
    with path.open('rb') as source:
        header = source.read(1024)
    if header.startswith(b'MZ') or header.startswith(b'\x7fELF'):
        raise ValueError('不能添加可执行文件')
    valid = (suffix == '.pdf' and header.lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'%PDF-')
             or suffix == '.png' and header.startswith(b'\x89PNG\r\n\x1a\n')
             or suffix in ('.jpg', '.jpeg') and header.startswith(b'\xff\xd8\xff')
             or suffix == '.webp' and header.startswith(b'RIFF') and header[8:12] == b'WEBP')
    if suffix == '.ofd':
        try:
            with zipfile.ZipFile(path) as archive:
                valid = 'OFD.xml' in archive.namelist() and len(archive.infolist()) <= 10000
        except zipfile.BadZipFile:
            valid = False
    if not valid:
        raise ValueError('附件内容与文件类型不符，支持 PDF、PNG、JPEG、WEBP 和 OFD')


def register(app):
    root = app.data_dir / 'expenses'
    records = root / 'items'
    attachments = root / 'attachments'
    archive = root / 'archive'
    no_links(root, app.data_dir)
    root.mkdir(exist_ok=True)
    for directory in (records, attachments):
        no_links(directory, root)
        directory.mkdir(exist_ok=True)
    lock = threading.RLock()
    ledger = Ledger(root)
    app.ledger = ledger

    def journal(item, previous=None):
        fields = ('title', 'date', 'amount', 'amount_minor', 'currency', 'entry_type', 'status', 'category', 'notes', 'project_id', 'created_at')
        before = previous or {}
        changes = {key: item[key] for key in fields if key in item and item.get(key) != before.get(key)}
        old_attachments = {value['id']: value for value in before.get('attachments', [])}
        new_attachments = {value['id']: value for value in item.get('attachments', [])}
        for aid, attached in new_attachments.items():
            if old_attachments.get(aid) != attached:
                path = root / attached['relative_path']
                if path.is_file():
                    ledger.put_blob(path.read_bytes(), attached['sha256'])
                changes['attachment:' + aid] = attached
        for aid in old_attachments.keys() - new_attachments.keys():
            changes['attachment:' + aid] = None
        if changes:
            return ledger.patch('entry', item['id'], changes)
        return ledger.get('entry', item['id'])

    def materialize():
        with lock:
            for entity in ledger.list_entities('entry', include_deleted=True):
                path = record_path(entity['id'])
                if entity['deleted']:
                    path.unlink(missing_ok=True)
                    continue
                item = {key: value for key, value in entity.items() if key not in ('heads', 'conflicts', 'deleted') and not key.startswith('attachment:')}
                item['attachments'] = [value for key, value in entity.items() if key.startswith('attachment:') and value is not None]
                item['attachments'].sort(key=lambda value: value['id'])
                for attached in item['attachments']:
                    target = owned_path(item['id'], attached)
                    if not target.exists():
                        try:
                            body = ledger.get_blob(attached['sha256'])
                        except FileNotFoundError:
                            # A legacy backup can contain metadata for a missing
                            # receipt. Preserve it; attachment opening reports it.
                            continue
                        if len(body) != attached['size']:
                            raise ValueError('附件大小校验失败')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(body)
                if path.exists():
                    previous = json.loads(path.read_text('utf-8'))
                    keys = (set(previous) | set(item)) - {'revision', 'updated_at'}
                    changed = any(previous.get(key) != item.get(key) for key in keys)
                    item['revision'] = max(item['revision'], previous.get('revision', 0) + (1 if changed else 0))
                atomic_json(path, item)

    def apply_remote(operations):
        # Download outside the gate, then publish operations and their visible rows
        # together. A local edit cannot accidentally parent an unseen remote edit.
        with getattr(app, 'data_lock', contextlib.nullcontext()), lock:
            ledger.ingest(operations)
            materialize()
    ledger.apply_remote = apply_remote

    def record_path(item_id):
        path = records / (identifier(item_id) + '.json')
        no_links(path, root)
        return path

    def read(item_id):
        path = record_path(item_id)
        if not path.is_file():
            raise ValueError('费用条目不存在')
        item = json.loads(path.read_text('utf-8'))
        if not isinstance(item, dict) or item.get('id') != item_id:
            raise ValueError('费用记录损坏')
        try:
            normalized_amount, minor = money(item['amount'])
            entry_type = item.get('entry_type', 'expense')
            if entry_type not in ENTRY_TYPES:
                raise ValueError()
            if (not isinstance(item['amount_minor'], int) or isinstance(item['amount_minor'], bool) or item['amount_minor'] != minor
                    or item['currency'] not in CURRENCIES
                    or not isinstance(item['revision'], int) or isinstance(item['revision'], bool) or item['revision'] < 1
                    or not isinstance(item['attachments'], list)):
                raise ValueError()
            iso_date(item['date'])
            item['amount'] = normalized_amount
            item['entry_type'] = entry_type
            item['status'] = normalized_status(item['status'], entry_type)
            item['project_id'] = item.get('project_id', DEFAULT_PROJECT)
            if entry_type == 'income' and minor == 0:
                item['validation_warning'] = '合并后的收入金额为零，请检查金额或收支类型'
        except (KeyError, ValueError, TypeError):
            raise ValueError('费用记录格式或金额已损坏，不能安全汇总') from None
        return item

    def expect(item, params):
        revision = params.get('revision')
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool) or revision != item['revision']):
            raise ValueError('费用条目已更新，请刷新后重试')

    def text_field(value, name, limit, empty=False):
        if not isinstance(value, str):
            raise ValueError(f'{name}应为文本')
        value = value.strip()
        if not empty and not value or len(value) > limit or '\0' in value:
            raise ValueError(f'{name}需为{0 if empty else 1}到{limit}个字符')
        return value

    def get(params):
        with lock:
            item = copy.deepcopy(read(params.get('id')))
            entity = ledger.get('entry', item['id'])
            return {**item, 'sync_heads': entity['heads'] if entity else [], 'sync_conflicts': entity['conflicts'] if entity else {}}

    def save(params):
        with lock:
            previous = read(params['id']) if params.get('id') else None
            if previous:
                expect(previous, params)
            item = copy.deepcopy(previous) if previous else {'id': uuid.uuid4().hex, 'revision': 0, 'created_at': time.time(), 'attachments': []}
            item.pop('validation_warning', None)
            item['title'] = text_field(params.get('title', item.get('title')), '标题', 200)
            item['date'] = iso_date(params.get('date', item.get('date')))
            item['amount'], item['amount_minor'] = money(params.get('amount', item.get('amount')))
            entry_type = params.get('entry_type', item.get('entry_type', 'expense'))
            if entry_type not in ENTRY_TYPES:
                raise ValueError('请选择支出或转账收入')
            item['entry_type'] = entry_type
            if entry_type == 'income' and item['amount_minor'] <= 0:
                raise ValueError('转账收入金额必须大于零')
            currency = params.get('currency', item.get('currency', 'CNY'))
            item['currency'] = str(currency).strip().upper()
            if item['currency'] not in CURRENCIES:
                raise ValueError('币种请选择 CNY、USD、EUR 或 HKD')
            status = params.get('status', item.get('status', 'waiting'))
            if entry_type == 'expense' and previous and previous.get('entry_type') == 'income' and status == 'not_applicable':
                status = 'waiting'
            item['status'] = normalized_status(status, entry_type)
            item['category'] = text_field(params.get('category', item.get('category', '转账收入' if entry_type == 'income' else 'AI订阅')), '分类', 80)
            item['notes'] = text_field(params.get('notes', item.get('notes', '')), '备注', 5000, empty=True)
            item['project_id'] = identifier(params.get('project_id', item.get('project_id', DEFAULT_PROJECT)))
            project = ledger.get('project', item['project_id'])
            if not project or project['deleted'] or project.get('archived') and (not previous or previous['project_id'] != item['project_id']):
                raise ValueError('请选择有效且未归档的项目')
            if previous and all(item[key] == previous.get(key) for key in ('title', 'date', 'amount', 'currency', 'entry_type', 'status', 'category', 'notes', 'project_id')):
                return copy.deepcopy(previous)
            item.update(revision=item['revision'] + 1, updated_at=time.time())
            journal(item, previous)
            atomic_json(record_path(item['id']), item)
            app.emit('expenses.changed', id=item['id'], month=item['date'][:7])
            return item

    def listing(params):
        start_date = end_date = None
        if 'start_date' in params or 'end_date' in params:
            if any(key in params for key in ('month', 'start_month', 'end_month')):
                raise ValueError('日期和月份区间不能同时使用')
            start_date, end_date = iso_date(params.get('start_date')), iso_date(params.get('end_date'))
            if start_date > end_date:
                raise ValueError('开始日期不能晚于结束日期')
            start, end = start_date[:7], end_date[:7]
        else:
            start, end = period(params)
        project_id = params.get('project_id', DEFAULT_PROJECT)
        project = None if project_id == 'all' else ledger.get('project', identifier(project_id))
        if project_id != 'all' and (not project or project['deleted']):
            raise ValueError('记账项目不存在')
        settlement_mode = project.get('settlement_mode', 'general') if project else 'general'
        filters = {}
        for field in ('query', 'entry_type', 'status', 'category', 'currency'):
            value = params.get(field)
            if value in (None, '') or field in ('entry_type', 'status', 'currency') and value == 'all':
                continue
            if not isinstance(value, str):
                raise ValueError('筛选条件应为文本')
            value = value.strip()
            if field == 'currency':
                value = value.upper()
                if value not in CURRENCIES:
                    raise ValueError('筛选币种无效')
            if field == 'entry_type' and value not in ENTRY_TYPES:
                raise ValueError('筛选收支类型无效')
            if field == 'status':
                value = LEGACY_STATUSES.get(value, value)
                if value not in (*STATUSES, 'not_applicable'):
                    raise ValueError('筛选报销状态无效')
            if field == 'category' and len(value) > 80 or field == 'query' and len(value) > 500:
                raise ValueError('筛选条件过长')
            if value:
                filters[field] = value
        with lock:
            items = []
            for path in records.glob('*.json'):
                try:
                    item = read(path.stem)
                except (OSError, ValueError):
                    raise ValueError('费用记录损坏，无法安全汇总：' + path.name) from None
                if (start <= item['date'][:7] <= end and (not start_date or start_date <= item['date'] <= end_date)
                        and (project_id == 'all' or item['project_id'] == project_id)):
                    items.append(item)
        items.sort(key=lambda item: (item['date'], item['created_at']), reverse=True)
        totals = {}
        for item in items:
            group = totals.setdefault(item['currency'], {name: 0 for name in ('expense_total', 'reimbursed', 'pending', 'non_reimbursable', 'transfer_income', 'count', 'expense_count', 'income_count')})
            if item['entry_type'] == 'income':
                group['transfer_income'] += item['amount_minor']
                group['income_count'] += 1
            else:
                group['expense_total'] += item['amount_minor']
                group['pending' if item['status'] == 'waiting' else item['status']] += item['amount_minor']
                group['expense_count'] += 1
            group['count'] += 1
        summary = []
        for currency in CURRENCIES:
            if currency not in totals:
                continue
            group = totals[currency]
            group['net'] = group['transfer_income'] + group['reimbursed'] - group['expense_total']
            # Split the whole period's unreimbursed cost once, rounding half a cent up.
            group['share_due'] = (group['expense_total'] - group['reimbursed'] + 1) // 2
            group['settlement_remaining'] = group['share_due'] - group['transfer_income']
            if settlement_mode != 'half':
                group['share_due'] = 0
                group['settlement_remaining'] = 0
            group.update(total=group['expense_total'], personal=group['non_reimbursable'], waiting=group['pending'],
                         unsubmitted=group['pending'], submitted=0)
            counts = ('count', 'expense_count', 'income_count')
            summary.append({'currency': currency, **{name: group[name] for name in counts},
                            **{name: formatted(value) for name, value in group.items() if name not in counts},
                            **{name + '_minor': value for name, value in group.items() if name not in counts}})
        categories = sorted({item['category'] for item in items}, key=str.casefold)
        def matches(item):
            for field, expected in filters.items():
                if field == 'query':
                    haystack = '\n'.join(item.get(name, '') for name in ('title', 'category', 'notes')).casefold()
                    if expected.casefold() not in haystack:
                        return False
                elif item[field] != expected:
                    return False
            return True
        return {'month': start if start == end else None, 'start_month': start, 'end_month': end,
                'start_date': start_date, 'end_date': end_date, 'project_id': project_id, 'settlement_mode': settlement_mode,
                'summary_scope': 'period', 'items': [item for item in items if matches(item)],
                'summary': summary, 'categories': categories}

    def owned_path(item_id, attached):
        aid = identifier(attached.get('id'))
        relative = attached.get('relative_path')
        if not isinstance(relative, str) or '\\' in relative:
            raise ValueError('附件路径无效')
        rel = PurePosixPath(relative)
        suffix = rel.suffix.lower()
        if suffix not in TYPES or rel.parts != ('attachments', identifier(item_id), aid + suffix):
            raise ValueError('附件路径不属于此费用条目')
        path = root.joinpath(*rel.parts)
        no_links(path, root)
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('附件路径越界')
        return path

    def selected(item, aid):
        aid = identifier(aid)
        attached = next((value for value in item['attachments'] if value['id'] == aid), None)
        if attached is None:
            raise ValueError('附件不属于此费用条目')
        return attached

    def attachment_get(params):
        with lock:
            item = read(params.get('id'))
            attached = selected(item, params.get('attachment_id'))
            path = owned_path(item['id'], attached)
            if not path.is_file() or path.stat().st_size != attached['size']:
                raise ValueError('附件文件缺失或大小发生变化')
            digest = hashlib.sha256()
            with path.open('rb') as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
            if digest.hexdigest() != attached['sha256']:
                raise ValueError('附件校验失败，文件可能已修改')
            return {'path': str(path), 'name': attached['original_name'], 'kind': attached['kind'], 'mime': TYPES[path.suffix.lower()]}

    def remove(params, attachment=False):
        with lock:
            item = read(params.get('id'))
            expect(item, params)
            value = selected(item, params.get('attachment_id')) if attachment else item
            if attachment:
                owned_path(item['id'], value)
            no_links(archive, root)
            archive.mkdir(exist_ok=True)
            destination = archive / (uuid.uuid4().hex + '.json')
            atomic_json(destination, {'deleted_at': time.time(), 'kind': 'attachment' if attachment else 'item', 'parent_id': item['id'] if attachment else None, 'record': value})
            if attachment:
                previous = copy.deepcopy(item)
                item['attachments'] = [entry for entry in item['attachments'] if entry['id'] != value['id']]
                item.update(revision=item['revision'] + 1, updated_at=time.time())
                journal(item, previous)
                atomic_json(record_path(item['id']), item)
            else:
                ledger.patch('entry', item['id'], {}, deleted=True)
                record_path(item['id']).unlink()
            app.emit('expenses.changed', id=item['id'], month=item['date'][:7])
            return {'ok': True, 'id': item['id'], 'archive_path': str(destination),
                    **({'attachment_id': value['id'], 'entry': item} if attachment else {})}

    def attach_job(job):
        if (not isinstance(job.params.get('revision'), int) or isinstance(job.params.get('revision'), bool)
                or job.params['revision'] < 1):
            raise ValueError('附件任务缺少有效条目版本，请从条目重新添加')
        if (not isinstance(job.params.get('paths'), list) or not 1 <= len(job.params['paths']) <= 100
                or any(not isinstance(value, str) for value in job.params['paths']) or job.params.get('kind') not in KINDS):
            raise ValueError('附件任务参数无效，请从条目重新添加')
        with lock:
            item = read(job.params['id'])
            expect(item, job.params)
        sources, total = [], 0
        for name in job.params['paths']:
            job.check_cancelled()
            if not isinstance(name, str):
                raise ValueError('附件文件路径无效')
            source = Path(name).expanduser().absolute()
            no_links(source)
            if not source.is_file() or source.suffix.lower() not in TYPES:
                raise ValueError('请选择已有的 PDF、PNG、JPEG、WEBP 或 OFD 文件')
            stat = source.stat()
            if not 0 < stat.st_size <= MAX_FILE_BYTES:
                raise ValueError('单个附件必须非空且不超过 50 MiB')
            total += stat.st_size
            if total > MAX_BATCH_BYTES:
                raise ValueError('单批附件不能超过 200 MiB')
            verify_type(source, source.suffix.lower())
            sources.append((source, stat))
        staged_values, created = [], []
        try:
            with tempfile.TemporaryDirectory(prefix='.incoming-', dir=root) as directory:
                stage = Path(directory)
                if not stage.resolve().is_relative_to(root.resolve()):
                    raise ValueError('附件暂存路径越界')
                copied = 0
                for source, before in sources:
                    aid = uuid.uuid4().hex
                    suffix = source.suffix.lower()
                    temporary = stage / (aid + suffix)
                    sha = hashlib.sha256()
                    file_bytes = 0
                    with source.open('rb') as incoming, temporary.open('xb') as output:
                        while block := incoming.read(1024 * 1024):
                            job.check_cancelled()
                            file_bytes += len(block)
                            if file_bytes > min(before.st_size, MAX_FILE_BYTES) or copied + len(block) > MAX_BATCH_BYTES:
                                raise ValueError('复制期间附件大小变化或超过限制，本批未添加')
                            sha.update(block)
                            output.write(block)
                            copied += len(block)
                            job.progress(min(85, copied / total * 85), '正在复制附件 ' + source.name)
                    no_links(source)
                    after = source.stat()
                    if file_bytes != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise ValueError('复制期间源附件发生变化，请重新添加')
                    verify_type(temporary, suffix)
                    staged_values.append({'id': aid, 'original_name': source.name, 'kind': job.params['kind'],
                                          'size': before.st_size, 'sha256': sha.hexdigest(), 'relative_path': f'attachments/{item["id"]}/{aid}{suffix}', 'created_at': time.time()})
                gate = getattr(app, 'data_lock', contextlib.nullcontext())
                with gate, lock:
                    try:
                        current = read(item['id'])
                        expect(current, job.params)
                    except ValueError as exc:
                        raise Cancelled('费用条目已编辑或删除，本批附件未写入') from exc
                    job.check_cancelled()
                    for attached in staged_values:
                        target = owned_path(item['id'], attached)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        # Exclusive publication cannot replace any existing file.
                        with target.open('xb') as output:
                            created.append(target)
                            with (stage / target.name).open('rb') as incoming:
                                while block := incoming.read(1024 * 1024):
                                    job.check_cancelled()
                                    output.write(block)
                    job.check_cancelled()
                    previous = copy.deepcopy(current)
                    current['attachments'].extend(staged_values)
                    current.update(revision=current['revision'] + 1, updated_at=time.time())
                    journal(current, previous)
                    atomic_json(record_path(current['id']), current)
                    if hasattr(job, 'mark_committed'):
                        job.mark_committed()
                    created.clear()
                app.emit('expenses.changed', id=current['id'], month=current['date'][:7])
                return {'id': current['id'], 'entry': current, 'attachments': staged_values}
        finally:
            for target in created:
                no_links(target, root)
                if target.resolve().is_relative_to(attachments.resolve()):
                    target.unlink(missing_ok=True)

    def attach(params):
        with lock:
            item = read(params.get('id'))
            expect(item, params)
            paths = params.get('paths')
            if not isinstance(paths, list) or not paths or len(paths) > 100 or any(not isinstance(path, str) for path in paths):
                raise ValueError('请选择 1 到 100 个附件')
            kind = params.get('kind', 'other')
            if kind not in KINDS:
                raise ValueError('附件类型请选择发票、收据、支付记录或其他')
            return app.jobs.submit('expenses.attach', {'id': item['id'], 'revision': item['revision'], 'paths': list(paths), 'kind': kind})

    app.jobs.register('expenses.attach', attach_job)
    for name, handler in (('list', listing), ('get', get), ('save', save), ('attach', attach), ('attachment', attachment_get)):
        app.register('expenses.' + name, handler)
    app.register('expenses.delete', remove)
    app.register('expenses.attachment.delete', lambda p: remove(p, attachment=True))
    # One-time migration preserves IDs, amounts, receipts and original AI settlement.
    def migrate():
        for path in records.glob('*.json'):
            if ledger.get('entry', path.stem) is None:
                item = read(path.stem)
                journal(item)
                atomic_json(path, item)
    app.ledger_migrate = migrate
    migrate()
    materialize()  # Recover durable edits after interruption before cache publication.
    register_service(app, ledger, materialize, listing, text_field, lock)
