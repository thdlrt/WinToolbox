"""Exact expense amounts and isolated owned-attachment workflows."""
import hashlib
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.features import expenses


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', register_live=False)
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def entry(app, **values):
    return app.call('expenses.save', {'title': 'AI subscription', 'date': '2026-09-10', 'amount': '12.30', **values})


def finish(app, job, expected='completed'):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = app.jobs.get(job['id'])
        if row['status'] not in ('queued', 'running', 'cancelling') and job['id'] not in app.jobs.active:
            assert row['status'] == expected, row
            return row['result'] if expected == 'completed' else row
        time.sleep(.01)
    raise AssertionError('Expense fixture timed out')


def pdf(path, body=b'fixture receipt'):
    path.write_bytes(b'%PDF-1.7\n' + body)
    return path


def add(app, item, paths, **params):
    return app.call('expenses.attach', {'id': item['id'], 'revision': item['revision'], 'paths': [str(path) for path in paths], 'kind': 'invoice', **params})


def test_month_totals_are_exact_and_currencies_never_mix(app):
    entry(app, amount='0.10')
    entry(app, amount='0.2', status='submitted')
    entry(app, amount='100.00', status='reimbursed')
    entry(app, amount='5', status='personal')
    entry(app, amount='20.50', currency='USD')
    entry(app, amount='999.00', date='2026-10-01')
    listed = app.call('expenses.list', {'month': '2026-09'})
    assert len(listed['items']) == 5
    cny, usd = listed['summary']
    assert cny['currency'] == 'CNY' and cny['total'] == '105.30' and cny['total_minor'] == 10530
    assert cny['pending'] == '0.30' and cny['pending_minor'] == 30 and cny['reimbursed'] == '100.00'
    assert cny['personal'] == '5.00' and cny['count'] == 4
    assert usd['currency'] == 'USD' and usd['total'] == '20.50' and usd['count'] == 1
    assert all(isinstance(row['amount'], str) and isinstance(row['amount_minor'], int) for row in listed['items'])
    assert app.call('expenses.list', {'month': '2026-11'})['summary'] == []


@pytest.mark.parametrize('amount', ['-1', '1.234', '1e3', 'NaN', 'Infinity', '1,000', '', 12.30, True, '1000000000.00'])
def test_invalid_amounts_are_rejected_without_creating_records(app, amount):
    with pytest.raises(ValueError):
        entry(app, amount=amount)
    assert not app.call('expenses.list', {'month': '2026-09'})['items']


def test_zero_and_editing_date_across_months_with_revision(app):
    item = entry(app, amount='0', date='2024-02-29')
    assert item['amount'] == '0.00' and item['amount_minor'] == 0
    updated = app.call('expenses.save', {'id': item['id'], 'revision': item['revision'], 'date': '2026-09-01', 'amount': '123.45'})
    assert updated['id'] == item['id'] and updated['revision'] == item['revision'] + 1
    assert not app.call('expenses.list', {'month': '2024-02'})['items']
    assert app.call('expenses.list', {'month': '2026-09'})['summary'][0]['total_minor'] == 12345
    with pytest.raises(ValueError, match='已更新'):
        app.call('expenses.save', {'id': item['id'], 'revision': item['revision'], 'notes': 'stale edit'})
    for date in ('2026-02-29', '2026-13-01', '20260901'):
        with pytest.raises(ValueError):
            entry(app, date=date)
    for month in ('2026-13', '2026-1', '../2026'):
        with pytest.raises(ValueError):
            app.call('expenses.list', {'month': month})
    with pytest.raises(ValueError):
        entry(app, currency='GBP')
    with pytest.raises(ValueError):
        entry(app, status='invalid')


def test_corrupt_record_stops_summary_instead_of_silently_losing_expense(app):
    item = entry(app)
    path = app.data_dir / 'expenses/items' / (item['id'] + '.json')
    corrupt = dict(item, amount_minor=99999)
    path.write_text(json.dumps(corrupt), encoding='utf-8')
    with pytest.raises(ValueError, match='无法安全汇总'):
        app.call('expenses.list', {'month': '2026-09'})
    with pytest.raises(ValueError, match='金额已损坏'):
        app.call('expenses.get', {'id': item['id']})


def test_multiple_attachments_copy_and_preserve_source_and_ownership(app, tmp_path):
    item = entry(app)
    first = pdf(tmp_path / '发票.pdf')
    second = tmp_path / '支付.png'
    second.write_bytes(b'\x89PNG\r\n\x1a\nfixture-image')
    originals = {path: path.read_bytes() for path in (first, second)}
    result = finish(app, add(app, item, [first, second]))
    assert result['entry']['revision'] == item['revision'] + 1
    assert len(result['attachments']) == 2
    other = entry(app, title='Different expense')
    for attached in result['attachments']:
        assert attached['kind'] == 'invoice' and len(attached['id']) == 32
        expected = 'attachments/' + item['id'] + '/' + attached['id']
        assert attached['relative_path'].startswith(expected) and not Path(attached['relative_path']).is_absolute()
        opened = app.call('expenses.attachment', {'id': item['id'], 'attachment_id': attached['id']})
        copied = Path(opened['path'])
        assert copied.is_relative_to(app.data_dir / 'expenses/attachments')
        source = first if attached['original_name'] == first.name else second
        assert copied.read_bytes() == originals[source]
        assert attached['sha256'] == hashlib.sha256(originals[source]).hexdigest()
        assert opened['name'] == source.name and attached['size'] == len(originals[source])
        with pytest.raises(ValueError, match='不属于'):
            app.call('expenses.attachment', {'id': other['id'], 'attachment_id': attached['id']})
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_ofd_and_webp_are_document_attachments_not_executed(app, tmp_path):
    item = entry(app)
    ofd = tmp_path / 'invoice.ofd'
    with zipfile.ZipFile(ofd, 'w') as archive:
        archive.writestr('OFD.xml', '<OFD/>')
    webp = tmp_path / 'receipt.webp'
    webp.write_bytes(b'RIFF\x10\x00\x00\x00WEBPfixture')
    result = finish(app, add(app, item, [ofd, webp], kind='receipt'))
    assert [a['kind'] for a in result['attachments']] == ['receipt', 'receipt']


def test_batch_failure_and_limits_leave_no_partial_metadata_or_files(app, tmp_path, monkeypatch):
    item = entry(app)
    good = pdf(tmp_path / 'good.pdf')
    disguised = tmp_path / 'bad.pdf'
    disguised.write_bytes(b'MZexecutable')
    finish(app, add(app, item, [good, disguised]), 'failed')
    assert app.call('expenses.get', {'id': item['id']})['attachments'] == []
    assert not list((app.data_dir / 'expenses/attachments').rglob('*.*'))
    monkeypatch.setattr(expenses, 'MAX_FILE_BYTES', 10)
    row = finish(app, add(app, item, [good]), 'failed')
    assert '50 MiB' in row['error']
    monkeypatch.setattr(expenses, 'MAX_FILE_BYTES', 100)
    monkeypatch.setattr(expenses, 'MAX_BATCH_BYTES', len(good.read_bytes()) + 1)
    row = finish(app, add(app, item, [good, good]), 'failed')
    assert '200 MiB' in row['error']
    assert good.read_bytes().startswith(b'%PDF')


def test_metadata_publish_failure_rolls_back_all_new_copies(app, tmp_path, monkeypatch):
    item = entry(app)
    sources = [pdf(tmp_path / name) for name in ('one.pdf', 'two.pdf')]
    original = expenses.atomic_json
    def fail(path, value):
        if path.name == item['id'] + '.json':
            raise OSError('fixture disk full')
        return original(path, value)
    monkeypatch.setattr(expenses, 'atomic_json', fail)
    finish(app, add(app, item, sources), 'failed')
    assert app.call('expenses.get', {'id': item['id']})['attachments'] == []
    assert not list((app.data_dir / 'expenses/attachments').rglob('*.pdf'))
    assert all(path.is_file() for path in sources)


def test_source_growing_during_copy_hits_stream_limit(app, tmp_path, monkeypatch):
    item = entry(app)
    source = pdf(tmp_path / 'growing.pdf')
    original = expenses.verify_type
    def grow(path, suffix):
        original(path, suffix)
        if path == source:
            with path.open('ab') as stream:
                stream.write(b'x' * 4096)
    monkeypatch.setattr(expenses, 'verify_type', grow)
    row = finish(app, add(app, item, [source]), 'failed')
    assert '大小变化或超过限制' in row['error']
    assert app.call('expenses.get', {'id': item['id']})['attachments'] == []


@pytest.mark.parametrize('action', ['edit', 'delete'])
def test_late_attachment_job_does_not_overwrite_or_revive_entry(app, tmp_path, monkeypatch, action):
    item = entry(app)
    source = pdf(tmp_path / 'invoice.pdf')
    entered, release = threading.Event(), threading.Event()
    original = expenses.verify_type
    def hold(path, suffix):
        original(path, suffix)
        if path.parent.name.startswith('.incoming-'):
            entered.set()
            assert release.wait(4)
    monkeypatch.setattr(expenses, 'verify_type', hold)
    job = add(app, item, [source])
    assert entered.wait(2)
    if action == 'edit':
        app.call('expenses.save', {'id': item['id'], 'revision': item['revision'], 'notes': 'Edited while copying'})
    else:
        app.call('expenses.delete', {'id': item['id'], 'revision': item['revision']})
    release.set()
    finish(app, job, 'cancelled')
    if action == 'edit':
        current = app.call('expenses.get', {'id': item['id']})
        assert current['notes'] == 'Edited while copying' and not current['attachments']
    else:
        assert app.call('expenses.list', {'month': '2026-09'})['items'] == []
    assert not list((app.data_dir / 'expenses/attachments').rglob('*.pdf'))
    assert source.is_file()


def test_delete_attachment_and_entry_archives_but_does_not_delete_files(app, tmp_path):
    item = entry(app)
    source = pdf(tmp_path / 'receipt.pdf')
    result = finish(app, add(app, item, [source]))
    attached = result['attachments'][0]
    copied = Path(app.call('expenses.attachment', {'id': item['id'], 'attachment_id': attached['id']})['path'])
    with pytest.raises(ValueError, match='已更新'):
        app.call('expenses.attachment.delete', {'id': item['id'], 'attachment_id': attached['id'], 'revision': item['revision']})
    removed = app.call('expenses.attachment.delete', {'id': item['id'], 'attachment_id': attached['id'], 'revision': result['entry']['revision']})
    assert Path(removed['archive_path']).is_file() and not removed['entry']['attachments']
    removed = app.call('expenses.delete', {'id': item['id'], 'revision': removed['entry']['revision']})
    assert json.loads(Path(removed['archive_path']).read_text('utf-8'))['record']['id'] == item['id']
    assert copied.is_file() and source.is_file()
    assert not app.call('expenses.list', {'month': '2026-09'})['items']


def test_attachment_paths_and_symbolic_sources_are_rejected(app, tmp_path, monkeypatch):
    item = entry(app)
    source = pdf(tmp_path / 'source.pdf')
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == source or original(path))
    finish(app, add(app, item, [source]), 'failed')
    monkeypatch.setattr(Path, 'is_symlink', original)
    result = finish(app, add(app, item, [source]))
    attached = result['attachments'][0]
    record = app.data_dir / 'expenses/items' / (item['id'] + '.json')
    changed = result['entry']
    changed['attachments'][0]['relative_path'] = '../../settings.json'
    record.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError, match='路径'):
        app.call('expenses.attachment', {'id': item['id'], 'attachment_id': attached['id']})
    with pytest.raises(ValueError, match='路径'):
        app.call('expenses.attachment.delete', {'id': item['id'], 'attachment_id': attached['id']})
    for method in ('expenses.get', 'expenses.delete'):
        with pytest.raises(ValueError):
            app.call(method, {'id': '../../settings'})


def test_direct_job_submission_cannot_bypass_revision_or_kind_validation(app, tmp_path):
    item = entry(app)
    source = pdf(tmp_path / 'invoice.pdf')
    for params in ({'id': item['id'], 'paths': [str(source)], 'kind': 'invoice'},
                   {'id': item['id'], 'revision': item['revision'], 'paths': [str(source)], 'kind': 'executable'}):
        finish(app, app.call('jobs.submit', {'tool': 'expenses.attach', 'params': params}), 'failed')
    assert app.call('expenses.get', {'id': item['id']})['attachments'] == []


def test_cancelled_copy_rolls_back_and_tampered_attachment_cannot_open(app, tmp_path, monkeypatch):
    item = entry(app)
    source = pdf(tmp_path / 'invoice.pdf')
    entered, release = threading.Event(), threading.Event()
    original = expenses.verify_type
    def hold(path, suffix):
        original(path, suffix)
        if path.parent.name.startswith('.incoming-'):
            entered.set()
            assert release.wait(4)
    monkeypatch.setattr(expenses, 'verify_type', hold)
    job = add(app, item, [source])
    assert entered.wait(2)
    app.call('jobs.cancel', {'id': job['id']})
    release.set()
    finish(app, job, 'cancelled')
    assert not app.call('expenses.get', {'id': item['id']})['attachments']
    assert not list((app.data_dir / 'expenses/attachments').rglob('*.pdf'))
    monkeypatch.setattr(expenses, 'verify_type', original)
    result = finish(app, add(app, item, [source]))
    attached = result['attachments'][0]
    path = Path(app.call('expenses.attachment', {'id': item['id'], 'attachment_id': attached['id']})['path'])
    old = path.read_bytes()
    path.write_bytes(old[:-1] + (b'x' if old[-1:] != b'x' else b'y'))
    with pytest.raises(ValueError, match='校验失败'):
        app.call('expenses.attachment', {'id': item['id'], 'attachment_id': attached['id']})
    assert source.read_bytes() == old
