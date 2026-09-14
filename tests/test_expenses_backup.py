"""Receipts and bookkeeping must travel with backups, even without media caches."""
import base64
import json
import time
import zipfile
from pathlib import Path

from toolbox.app import App


def finish(app, job):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = app.jobs.get(job['id'])
        if record['status'] not in ('running', 'queued', 'cancelling'):
            assert record['status'] == 'completed', record.get('error')
            while job['id'] in app.jobs.active:
                time.sleep(.01)
            return record['result']
        time.sleep(.02)
    raise AssertionError('Backup task timed out')


def test_receipts_restore_without_media_and_use_destination_paths(tmp_path):
    source = App(tmp_path / 'source', register_live=False)
    target = App(tmp_path / 'target', register_live=False)
    try:
        invoice = tmp_path / 'invoice.pdf'
        invoice.write_bytes(b'%PDF-1.4\n% fixture invoice\n%%EOF\n')
        payment = tmp_path / 'payment.png'
        payment.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFOsAAAAASUVORK5CYII='))
        entry = source.call('expenses.save', {'title': 'AI subscription', 'date': '2026-09-10',
                            'amount': '20.00', 'currency': 'USD', 'category': 'AI订阅', 'status': 'submitted'})
        finish(source, source.call('expenses.attach', {'id': entry['id'], 'paths': [str(invoice)], 'kind': 'invoice'}))
        finish(source, source.call('expenses.attach', {'id': entry['id'], 'paths': [str(payment)], 'kind': 'payment'}))
        before = target.call('expenses.save', {'title': 'Local fixture', 'date': '2026-09-01', 'amount': '1.23'})
        path = tmp_path / 'expenses.wtbak'
        finish(source, source.call('backups.export', {'path': str(path), 'include_media': False}))
        finish(target, target.call('backups.import', {'path': str(path)}))
        items = target.call('expenses.list', {'month': '2026-09'})['items']
        assert {item['id'] for item in items} == {entry['id']}
        assert before['id'] not in {item['id'] for item in items}
        restored = target.call('expenses.get', {'id': entry['id']})
        assert restored['amount'] == '20.00'
        assert len(restored['attachments']) == 2
        for attached in restored['attachments']:
            result = target.call('expenses.attachment', {'id': entry['id'], 'attachment_id': attached['id']})
            copied = Path(result['path'])
            assert copied.resolve().is_relative_to(target.data_dir / 'expenses')
            original = invoice if attached['kind'] == 'invoice' else payment
            assert copied.read_bytes() == original.read_bytes()
    finally:
        for app in (source, target):
            app.close()
            app.jobs.pool.shutdown(wait=True)


def test_empty_ledger_snapshot_clears_destination_entries(tmp_path):
    source = App(tmp_path / 'source', register_live=False)
    target = App(tmp_path / 'target', register_live=False)
    try:
        target.call('expenses.save', {'title': 'Local fixture', 'date': '2026-09-01', 'amount': '1.00'})
        package = tmp_path / 'empty-ledger.wtbak'
        finish(source, source.call('backups.export', {'path': str(package), 'include_media': False}))
        finish(target, target.call('backups.import', {'path': str(package)}))
        assert target.call('expenses.list', {'month': '2026-09'})['items'] == []
        # Restoring an empty managed root omits its empty nested folders. New
        # attachments must work immediately, without restarting the application.
        entry = target.call('expenses.save', {'title': 'After restore', 'date': '2026-09-02', 'amount': '2.00'})
        receipt = tmp_path / 'after-restore.pdf'
        receipt.write_bytes(b'%PDF-1.4\n% fixture receipt\n%%EOF\n')
        result = finish(target, target.call('expenses.attach', {'id': entry['id'], 'paths': [str(receipt)], 'kind': 'receipt'}))
        assert len(result['entry']['attachments']) == 1
    finally:
        for app in (source, target):
            app.close()
            app.jobs.pool.shutdown(wait=True)


def test_older_snapshot_without_ledger_scope_preserves_new_ledger(tmp_path):
    source = App(tmp_path / 'source', register_live=False)
    target = App(tmp_path / 'target', register_live=False)
    try:
        entry = target.call('expenses.save', {'title': 'Keep newer module data', 'date': '2026-09-01', 'amount': '2.00'})
        current = tmp_path / 'current.wtbak'
        older = tmp_path / 'older.wtbak'
        finish(source, source.call('backups.export', {'path': str(current)}))
        with zipfile.ZipFile(current) as incoming, zipfile.ZipFile(older, 'w') as outgoing:
            manifest = json.loads(incoming.read('backup-manifest.json'))
            manifest['directories'].remove('expenses')
            assert not any(name.startswith('expenses/') for name in manifest['files'])
            for name in incoming.namelist():
                if name == 'backup-manifest.json':
                    outgoing.writestr(name, json.dumps(manifest))
                elif not name.startswith('expenses/'):
                    outgoing.writestr(name, incoming.read(name))
        finish(target, target.call('backups.import', {'path': str(older)}))
        assert target.call('expenses.get', {'id': entry['id']})['amount'] == '2.00'
    finally:
        for app in (source, target):
            app.close()
            app.jobs.pool.shutdown(wait=True)


def test_income_and_legacy_status_keep_range_totals_after_restore(tmp_path):
    source = App(tmp_path / 'source', register_live=False)
    target = App(tmp_path / 'target', register_live=False)
    try:
        legacy = source.call('expenses.save', {'title': 'Shared subscription', 'date': '2025-12-31', 'amount': '50.10'})
        file = source.data_dir / 'expenses/items' / (legacy['id'] + '.json')
        record = json.loads(file.read_text('utf-8'))
        record.pop('entry_type', None)
        record['status'] = 'submitted'
        file.write_text(json.dumps(record), encoding='utf-8')
        original_bytes = file.read_bytes()
        source.call('expenses.save', {'title': 'Reimbursed subscription', 'date': '2026-01-01', 'amount': '10.00', 'status': 'reimbursed'})
        income = source.call('expenses.save', {'title': 'Shared costs transfer', 'date': '2026-01-31', 'amount': '30.00', 'entry_type': 'income'})
        payment = tmp_path / 'transfer.pdf'
        payment.write_bytes(b'%PDF-1.4\n% fixture transfer\n%%EOF\n')
        finish(source, source.call('expenses.attach', {'id': income['id'], 'paths': [str(payment)], 'kind': 'payment'}))
        request = {'start_month': '2025-12', 'end_month': '2026-01'}
        initial = source.call('expenses.list', request)
        assert file.read_bytes() == original_bytes  # Reading does not migrate files.
        package = tmp_path / 'income-ledger.wtbak'
        finish(source, source.call('backups.export', {'path': str(package), 'include_media': False}))
        finish(target, target.call('backups.import', {'path': str(package)}))
        restored = target.call('expenses.list', {**request, 'entry_type': 'income'})
        assert restored['summary'] == initial['summary']
        totals = restored['summary'][0]
        assert (totals['expense_total'], totals['reimbursed'], totals['transfer_income'], totals['net']) == ('60.10', '10.00', '30.00', '-20.10')
        assert (totals['share_due'], totals['settlement_remaining']) == ('25.05', '-4.95')
        assert [row['id'] for row in restored['items']] == [income['id']]
        assert target.call('expenses.get', {'id': legacy['id']})['status'] == 'waiting'
        attachment = restored['items'][0]['attachments'][0]
        path = target.call('expenses.attachment', {'id': income['id'], 'attachment_id': attachment['id']})['path']
        assert Path(path).is_relative_to(target.data_dir)
        assert Path(path).read_bytes() == payment.read_bytes()
    finally:
        for app in (source, target):
            app.close()
            app.jobs.pool.shutdown(wait=True)
