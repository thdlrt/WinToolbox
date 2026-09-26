"""Desktop ledger project, sync lifecycle and spreadsheet export APIs."""
import contextlib
import csv
import datetime
from decimal import Decimal
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from .ledger import DEFAULT_PROJECT, ID
from .ledger_sync import LedgerRemote
from .settings import atomic_json
from .webdav_layout import shared_config, service_paths, ledger_sources, ledger_source_result, migration_config


def write_xlsx(path, rows):
    """Small standards-compliant workbook; untrusted text is always inlineStr."""
    xml_rows = []
    for number, row in enumerate(rows, 1):
        cells = []
        for index, value in enumerate(row):
            column = chr(65 + index)
            clean = ''.join(c for c in str(value) if c in '\t\n\r' or ord(c) >= 32)
            if number > 1 and index == 6:
                cells.append(f'<c r="{column}{number}" s="2"><v>{clean}</v></c>')
            elif number > 1 and index == 1:
                serial = (datetime.date.fromisoformat(clean) - datetime.date(1899, 12, 30)).days
                cells.append(f'<c r="{column}{number}" s="3"><v>{serial}</v></c>')
            else:
                style = ' s="1"' if number == 1 else ''
                cells.append(f'<c r="{column}{number}" t="inlineStr"{style}><is><t xml:space="preserve">{escape(clean)}</t></is></c>')
        xml_rows.append(f'<row r="{number}">' + ''.join(cells) + '</row>')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="记账明细" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/><xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="3" width="16" customWidth="1"/><col min="4" max="5" width="26" customWidth="1"/><col min="6" max="8" width="15" customWidth="1"/><col min="9" max="11" width="40" customWidth="1"/></cols><sheetData>' + ''.join(xml_rows) + f'</sheetData><autoFilter ref="A1:K{len(rows)}"/></worksheet>')


def register_service(app, ledger, materialize, listing, text_field, expense_lock):
    stop, wake = threading.Event(), threading.Event()
    sync_lock, state_lock = threading.Lock(), threading.RLock()
    state_path = ledger.root / 'ledger' / 'sync-state.json'
    state = json.loads(state_path.read_text('utf-8')) if state_path.exists() else {}
    state.update(syncing=False)
    worker = None
    paused = False

    def config():
        return shared_config(app.data_dir)

    def configured():
        value = config()
        return bool(value.get('url') and value.get('username') and value.get('password_dpapi'))

    def status(_=None):
        with state_lock:
            count = len(ledger.export_operations())
            pending_sources = [value for value in ledger_sources(app.data_dir).values() if value.get('pending')]
            return {'configured': configured(), 'syncing': state.get('syncing', False),
                    'pending': max(0, count - state.get('synced_operations', 0)),
                    'last_sync': state.get('last_sync'), 'error': state.get('error'),
                    'conflicts': len(ledger.conflicts()), 'incomplete': ledger.incomplete(),
                    'migration_pending': len(pending_sources), 'migration_error': '\n'.join(value['error'] for value in pending_sources if value.get('error')) or None,
                    'remote_path': service_paths(config())['ledger']}

    def projects(_):
        return {'items': ledger.list_entities('project')}

    def project_save(params):
        entity_id = params.get('id') or uuid.uuid4().hex
        if not isinstance(entity_id, str) or not ID.fullmatch(entity_id):
            raise ValueError('项目标识无效')
        existing = ledger.get('project', entity_id)
        if params.get('id') and not existing:
            raise ValueError('项目不存在')
        name = text_field(params.get('name', (existing or {}).get('name')), '项目名称', 100)
        mode = params.get('settlement_mode', (existing or {}).get('settlement_mode', 'general'))
        archived = params.get('archived', (existing or {}).get('archived', False))
        if mode not in ('general', 'half') or type(archived) is not bool:
            raise ValueError('项目结算方式或归档状态无效')
        changes = {'name': name, 'settlement_mode': mode, 'archived': archived}
        if existing:
            changes = {key: value for key, value in changes.items() if existing.get(key) != value}
        if not existing:
            changes['created_at'] = time.time()
        return ledger.patch('project', entity_id, changes, parents=params.get('heads'))

    def resolve(params):
        kind, entity_id = params.get('entity_type'), params.get('id')
        current = ledger.get(kind, entity_id)
        if not current or not isinstance(params.get('changes'), dict) or not isinstance(params.get('heads'), list):
            raise ValueError('请选择冲突版本并刷新条目后重试')
        changes = dict(params['changes'])
        if not changes or any(field not in current['conflicts'] for field in changes):
            raise ValueError('只能解决当前存在的冲突字段')
        # Resolving chooses an actually retained candidate; arbitrary edits use save.
        for field, value in changes.items():
            if not any(candidate['value'] == value for candidate in current['conflicts'][field]):
                raise ValueError('所选冲突版本不存在')
        if 'amount' in changes:
            changes['amount_minor'] = int(Decimal(changes['amount']) * 100)
        with getattr(app, 'data_lock', contextlib.nullcontext()), expense_lock:
            result = ledger.patch(kind, entity_id, changes, parents=params['heads'])
            materialize()
        app.emit('expenses.changed', id=entity_id)
        return result

    def sync_job(job):
        if not sync_lock.acquire(blocking=False):
            return {'skipped': True, 'reason': 'already_syncing'}
        with state_lock:
            state.update(syncing=True, error=None)
        remote = None
        try:
            if getattr(app, 'maintenance', False) or paused:
                return {'skipped': True, 'reason': 'maintenance'}
            if not configured():
                raise ValueError('请先在设置中配置统一 WebDAV 连接')
            migration_errors = []
            for key, source in ledger_sources(app.data_dir).items():
                if not source.get('pending'):
                    continue
                old_remote = None
                try:
                    job.check_cancelled()
                    old_remote = LedgerRemote(migration_config(source['config'], config()))
                    old_remote.sync(ledger, job, read_only=True)
                    ledger_source_result(app.data_dir, key)
                except Exception as exc:
                    error = '旧 WebDAV 记账数据尚未合并，将继续重试：' + str(exc)
                    migration_errors.append(error)
                    ledger_source_result(app.data_dir, key, error)
                finally:
                    if old_remote:
                        old_remote.close()
            remote = LedgerRemote(config())
            # No network I/O holds the app data lock; local writing remains available.
            result = remote.sync(ledger, job)
            with getattr(app, 'data_lock', contextlib.nullcontext()), expense_lock:
                materialize()
            with state_lock:
                state.update(last_sync=time.time(), synced_operations=result['synced_operations'], error='\n'.join(migration_errors) or None)
            result['migration_pending'] = len(migration_errors)
            app.emit('expenses.changed', synced=True)
            return result
        except Exception as exc:
            with state_lock:
                state['error'] = str(exc)
            raise
        finally:
            if remote:
                remote.close()
            with state_lock:
                state['syncing'] = False
                atomic_json(state_path, state)
            sync_lock.release()
            app.emit('expenses.sync.changed', **status())

    def submit(_=None):
        return app.jobs.submit('expenses.sync', {})

    def loop():
        while not stop.is_set():
            wake.wait(60)
            wake.clear()
            if stop.is_set():
                break
            try:
                if not paused and not getattr(app, 'maintenance', False) and configured() and not status()['syncing']:
                    submit()
            except Exception:
                # Offline configuration and shutdown races are retried on next wake.
                pass

    def start():
        nonlocal worker
        with state_lock:
            if worker is None:
                worker = threading.Thread(target=loop, name='ledger-auto-sync', daemon=True)
                worker.start()
        wake.set()

    def close():
        stop.set()
        wake.set()
        if worker is not None:
            worker.join(timeout=2)

    def before_restore():
        nonlocal paused
        paused = True

    def after_restore():
        nonlocal paused
        from .ledger import Ledger
        restored = Ledger(ledger.root)
        with ledger.lock:
            ledger.operations = restored.operations
        app.ledger_migrate()
        with state_lock:
            state.clear()
            state['syncing'] = False
        paused = False
        wake.set()

    def export_job(job):
        with getattr(app, 'data_lock', contextlib.nullcontext()), expense_lock:
            result = listing(job.params)
            projects_by_id = {v['id']: v['name'] for v in ledger.list_entities('project', include_deleted=True)}
        rows = [['项目', '日期', '收支类型', '标题', '分类', '币种', '金额', '报销状态', '备注', '附件名称', '条目标识']]
        statuses = {'waiting': '等待报销', 'reimbursed': '已报销', 'non_reimbursable': '不可报销', 'not_applicable': '不适用'}
        for item in result['items']:
            job.check_cancelled()
            rows.append([projects_by_id.get(item['project_id'], item['project_id']), item['date'],
                         '收入' if item['entry_type'] == 'income' else '支出', item['title'], item['category'],
                         item['currency'], item['amount'], statuses[item['status']], item['notes'],
                         '; '.join(a['original_name'] for a in item['attachments']), item['id']])
        folder = app.data_dir / 'exports' / 'ledger'
        folder.mkdir(parents=True, exist_ok=True)
        fmt = job.params['format']
        path = folder / ('记账-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.' + fmt)
        if fmt == 'xlsx':
            write_xlsx(path, rows)
        else:
            with path.open('w', encoding='utf-8-sig', newline='') as output:
                csv.writer(output).writerows([["'" + value if str(value).lstrip().startswith(('=', '+', '-', '@')) else value for value in row] for row in rows])
        job.artifact(path, fmt, path.name)
        job.progress(100, '记账表格已导出')
        return {'path': str(path), 'rows': len(rows) - 1, 'format': fmt}

    def export(params):
        if params.get('format', 'xlsx') not in ('xlsx', 'csv'):
            raise ValueError('请选择 XLSX 或 CSV 格式')
        listing(params)  # Validate project/date inputs before queuing.
        return app.jobs.submit('expenses.export', {**params, 'format': params.get('format', 'xlsx')})

    ledger.on_change = wake.set
    app.ledger_auto_sync = start
    app.ledger_close = close
    app.ledger_before_restore = before_restore
    app.ledger_after_restore = after_restore
    app.jobs.register('expenses.sync', sync_job)
    app.jobs.register('expenses.export', export_job)
    for name, handler in (('projects.list', projects), ('projects.save', project_save),
                          ('sync.status', status), ('sync', submit), ('export', export),
                          ('conflicts', lambda _: {'items': ledger.conflicts()}), ('resolve', resolve)):
        app.register('expenses.' + name, handler)
