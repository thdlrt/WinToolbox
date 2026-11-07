"""Per-entry participation is independent of reimbursements and journal merges."""
import csv
import json
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.ledger import Ledger


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', register_live=False)
    value.data_sync.stop_worker()
    yield value
    value.close()
    value.jobs.pool.shutdown(wait=True)


def save(app, **fields):
    return app.call('expenses.save', {'title': 'Fixture', 'date': '2026-10-01', 'amount': '10.00', **fields})


def totals(app, **params):
    return app.call('expenses.list', {'month': '2026-10', **params})['summary'][0]


def finish(app, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = app.jobs.get(job['id'])
        if result['status'] not in ('queued', 'running', 'cancelling'):
            assert result['status'] == 'completed', result
            return result['result']
        time.sleep(.01)
    raise AssertionError('Job timed out')


def test_excluded_expenses_and_their_reimbursements_do_not_change_settlement(app):
    save(app, amount='100.01')
    save(app, amount='20.02', status='reimbursed')
    own = save(app, amount='300.01', split_enabled=False)
    save(app, amount='10.03', status='reimbursed', split_enabled=False)
    save(app, amount='20.00', entry_type='income')
    result = totals(app)
    assert (result['expense_total'], result['reimbursed']) == ('430.07', '30.05')
    assert (result['share_base'], result['excluded_expense_total']) == ('100.01', '310.04')
    assert (result['share_due'], result['settlement_remaining']) == ('50.01', '30.01')
    assert totals(app, query='nothing') == result, 'list filters never change settlement'
    app.call('expenses.save', {'id': own['id'], 'status': 'reimbursed'})
    assert totals(app)['share_due'] == '50.01', 'personal reimbursement must not deduct shared costs'
    updated = app.call('expenses.save', {'id': own['id'], 'status': 'waiting', 'split_enabled': True})
    assert updated['revision'] > own['revision']
    assert (totals(app)['share_due'], totals(app)['settlement_remaining']) == ('200.01', '180.01')


def test_participating_costs_are_rounded_once_and_other_project_modes_unchanged(app):
    save(app, amount='0.01')
    save(app, amount='0.01')
    save(app, amount='0.01', split_enabled=False)
    assert totals(app)['share_due_minor'] == 1
    project = app.call('expenses.projects.save', {'name': '普通项目'})
    save(app, project_id=project['id'], split_enabled=False)
    assert totals(app, project_id=project['id'])['share_due_minor'] == 0


@pytest.mark.parametrize('flag', [None, 0, 1, 'false', [], {}])
def test_invalid_flag_cannot_be_silently_coerced(app, flag):
    with pytest.raises(ValueError, match='均摊'):
        save(app, split_enabled=flag)
    assert not app.ledger.export_operations()


def test_legacy_missing_flag_stays_shared_without_rewriting_history(app):
    entry = save(app)
    path = app.data_dir / 'expenses/items' / (entry['id'] + '.json')
    value = json.loads(path.read_text('utf-8'))
    value.pop('split_enabled', None)
    path.write_text(json.dumps(value), encoding='utf-8')
    before = path.read_bytes()
    count = len(app.ledger.export_operations())
    assert app.call('expenses.get', {'id': entry['id']})['split_enabled'] is True
    app.call('expenses.save', {'id': entry['id'], 'split_enabled': True})
    assert path.read_bytes() == before and len(app.ledger.export_operations()) == count


def test_atomic_commit_and_sync_preserve_flag_and_attachments(app, tmp_path):
    receipt = tmp_path / 'receipt.pdf'
    receipt.write_bytes(b'%PDF-1.4\nFixture\n%%EOF')
    entry = finish(app, app.call('expenses.commit', {
        'title': 'Personal subscription', 'date': '2026-10-01', 'amount': '99.00', 'split_enabled': False,
        'attachments_add': [{'path': str(receipt), 'kind': 'receipt'}], 'request_id': str(uuid.uuid4()),
    }))['entry']
    offline = Ledger(tmp_path / 'offline')
    offline.ingest(app.ledger.export_operations())
    assert offline.get('entry', entry['id'])['split_enabled'] is False
    offline.patch('entry', entry['id'], {'notes': 'Mobile edit'})
    app.ledger.apply_remote(offline.export_operations())
    value = app.call('expenses.get', {'id': entry['id']})
    assert value['split_enabled'] is False and value['notes'] == 'Mobile edit'
    assert value['attachments'] == entry['attachments']
    assert Ledger(app.data_dir / 'expenses').get('entry', entry['id'])['split_enabled'] is False
    update = finish(app, app.call('expenses.commit', {
        'id': value['id'], 'revision': value['revision'], 'split_enabled': True,
        'request_id': str(uuid.uuid4()),
    }))['entry']
    assert update['split_enabled'] is True and update['attachments'] == entry['attachments']
    assert app.ledger.get('entry', entry['id'])['split_enabled'] is True


def test_export_includes_participation_without_changing_numeric_columns(app):
    shared = save(app, title='Shared')
    own = save(app, title='Personal', split_enabled=False)
    save(app, title='Income', entry_type='income')
    result = finish(app, app.call('expenses.export', {'month': '2026-10', 'format': 'csv'}))
    with Path(result['path']).open(encoding='utf-8-sig', newline='') as source:
        rows = list(csv.reader(source))
    assert rows[0][-1] == '参与均摊'
    values = {row[3]: row for row in rows[1:]}
    assert values['Shared'][-1] == '是' and values['Personal'][-1] == '否' and values['Income'][-1] == '不适用'
    assert values['Shared'][10] == shared['id'] and values['Personal'][10] == own['id']
    result = finish(app, app.call('expenses.export', {'month': '2026-10', 'format': 'xlsx'}))
    with zipfile.ZipFile(result['path']) as source:
        xml = source.read('xl/worksheets/sheet1.xml').decode()
    assert '参与均摊' in xml and '不适用' in xml and '<c r="G2" s="2"><v>10.00</v></c>' in xml
