"""Attachment category edits and one-click merged PDF export for expense entries."""
import copy
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from toolbox.app import App


class Job:
    def __init__(self, params):
        self.params = copy.deepcopy(params)
        self.artifacts = []
        self.progress_value = 0
        self.message = ''

    def check_cancelled(self):
        return None

    def progress(self, value, message=''):
        self.progress_value, self.message = value, message

    def artifact(self, path, *args, **kwargs):
        self.artifacts.append(str(path))


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', register_live=False)
    instance.data_sync.stop_worker()
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def finish(app, job, expected='completed'):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = app.jobs.get(job['id'])
        if row['status'] not in ('queued', 'running', 'cancelling') and job['id'] not in app.jobs.active:
            assert row['status'] == expected, row
            return row['result'] if expected == 'completed' else row
        time.sleep(.01)
    raise AssertionError('Expense fixture timed out')


def entry(app, **values):
    return app.call('expenses.save', {'title': 'GPT pro', 'date': '2026-10-27', 'amount': '780.00', **values})


def commit(app, params):
    return app.jobs.runners['expenses.commit'](Job(params))['entry']


def draft(app, item, **extra):
    return {'id': item['id'], 'revision': item['revision'], 'title': item['title'], 'date': item['date'],
            'amount': item['amount'], 'currency': item['currency'], 'entry_type': item['entry_type'],
            'status': item['status'], 'category': item['category'], 'notes': item['notes'],
            'project_id': item['project_id'], 'split_enabled': item['split_enabled'],
            'attachments_add': [], 'attachments_remove': [], 'attachments_kind': [],
            'request_id': str(uuid.uuid4()), **extra}


def source_pdf(tmp_path, name='receipt.pdf', pages=1):
    import pypdfium2 as pdfium
    path = tmp_path / name
    document = pdfium.PdfDocument.new()
    for _ in range(pages):
        document.new_page(300, 400)
    document.save(str(path))
    document.close()
    return path


def source_image(tmp_path, name='photo.png', size=(240, 320), mode='RGB'):
    from PIL import Image
    path = tmp_path / name
    Image.new(mode, size, (200, 30, 30)).save(path)
    return path


def source_ofd(tmp_path, name='invoice.ofd'):
    path = tmp_path / name
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('OFD.xml', '<OFD/>')
    return path


def attach(app, item, paths, kind='invoice'):
    job = app.call('expenses.attach', {'id': item['id'], 'revision': item['revision'],
                                       'paths': [str(path) for path in paths], 'kind': kind})
    return finish(app, job)['entry']


def test_commit_changes_saved_attachment_category_and_journals_it(app, tmp_path):
    item = attach(app, entry(app), [source_pdf(tmp_path)])
    aid = item['attachments'][0]['id']
    assert item['attachments'][0]['kind'] == 'invoice'
    updated = commit(app, draft(app, item, attachments_kind=[{'id': aid, 'kind': 'payment'}]))
    assert updated['attachments'][0]['kind'] == 'payment'
    assert updated['revision'] == item['revision'] + 1
    entity = app.ledger.get('entry', item['id'])
    assert entity['attachment:' + aid]['kind'] == 'payment'
    assert app.call('expenses.get', {'id': item['id']})['attachments'][0]['kind'] == 'payment'
    # Selecting the current category again is a no-op edit that still validates.
    same = commit(app, draft(app, updated, attachments_kind=[{'id': aid, 'kind': 'payment'}]))
    assert same['attachments'][0]['kind'] == 'payment'


@pytest.mark.parametrize('changes', [
    [{'id': 'x', 'kind': 'payment'}],
    [{'id': '0' * 32, 'kind': 'payment'}],
    [{'id': '0' * 32, 'kind': 'voucher'}],
    [{'kind': 'payment'}],
    ['payment'],
])
def test_commit_rejects_malformed_category_changes(app, tmp_path, changes):
    item = attach(app, entry(app), [source_pdf(tmp_path)])
    with pytest.raises(ValueError):
        commit(app, draft(app, item, attachments_kind=changes))
    assert app.call('expenses.get', {'id': item['id']})['attachments'][0]['kind'] == 'invoice'


def test_commit_rejects_category_change_for_removed_or_duplicate_attachment(app, tmp_path):
    item = attach(app, entry(app), [source_pdf(tmp_path), source_image(tmp_path)])
    first, second = (attached['id'] for attached in item['attachments'])
    with pytest.raises(ValueError):
        commit(app, draft(app, item, attachments_remove=[first], attachments_kind=[{'id': first, 'kind': 'other'}]))
    with pytest.raises(ValueError):
        commit(app, draft(app, item, attachments_kind=[{'id': first, 'kind': 'other'}, {'id': first, 'kind': 'receipt'}]))
    with pytest.raises(ValueError):
        commit(app, draft(app, item, attachments_kind=[{'id': second, 'kind': 'other'}, {'id': uuid.uuid4().hex, 'kind': 'receipt'}]))
    assert app.call('expenses.get', {'id': item['id']})['attachments'][0]['kind'] == 'invoice'


def test_merge_job_combines_images_and_pdf_pages_and_skips_ofd(app, tmp_path):
    import pypdfium2 as pdfium
    item = attach(app, entry(app), [source_image(tmp_path), source_pdf(tmp_path, 'two.pdf', pages=2),
                                     source_image(tmp_path, 'scan-alpha.png', size=(400, 300), mode='RGBA'),
                                     source_ofd(tmp_path)])
    job = Job({'id': item['id']})
    result = app.jobs.runners['expenses.attachments.merge'](job)
    assert result['merged'] == 3 and result['pages'] == 4 and result['skipped'] == ['invoice.ofd']
    path = Path(result['path'])
    assert path.is_file() and path.suffix == '.pdf'
    assert path.is_relative_to(app.data_dir / 'exports' / 'attachments')
    assert job.artifacts == [str(path)] and job.progress_value == 100
    with pdfium.PdfDocument(str(path)) as merged:
        assert len(merged) == 4
        width, height = merged.get_page(0).get_size()
        assert width <= 595.3 and height <= 841.9  # A4 bound with margins


def test_merge_job_honours_selection_and_order(app, tmp_path):
    import pypdfium2 as pdfium
    item = attach(app, entry(app), [source_pdf(tmp_path, 'a.pdf', pages=3), source_image(tmp_path, 'b.png')])
    pdf_id, image_id = (attached['id'] for attached in item['attachments'])
    result = app.jobs.runners['expenses.attachments.merge'](Job({'id': item['id'], 'attachment_ids': [image_id]}))
    assert result['merged'] == 1 and result['pages'] == 1
    with pdfium.PdfDocument(result['path']) as merged:
        assert len(merged) == 1
    with pytest.raises(ValueError):
        app.jobs.runners['expenses.attachments.merge'](Job({'id': item['id'], 'attachment_ids': [pdf_id, pdf_id]}))
    with pytest.raises(ValueError):
        app.jobs.runners['expenses.attachments.merge'](Job({'id': item['id'], 'attachment_ids': [uuid.uuid4().hex]}))


def test_merge_rejects_entries_without_mergeable_attachments(app, tmp_path):
    item = attach(app, entry(app), [source_ofd(tmp_path)])
    with pytest.raises(ValueError):
        app.jobs.runners['expenses.attachments.merge'](Job({'id': item['id']}))
    empty = entry(app, title='no attachments')
    with pytest.raises(ValueError):
        app.call('expenses.attachments.merge', {'id': empty['id']})


def test_merge_rpc_runs_as_tracked_job_with_artifact(app, tmp_path):
    item = attach(app, entry(app), [source_image(tmp_path), source_pdf(tmp_path)])
    result = finish(app, app.call('expenses.attachments.merge', {'id': item['id']}))
    assert result['pages'] == 2 and Path(result['path']).is_file()
    rows = [row for row in app.jobs.list() if row['tool'] == 'expenses.attachments.merge']
    assert len(rows) == 1 and rows[0]['artifacts'][0]['path'] == result['path']


def test_merge_detects_tampered_attachment_copy(app, tmp_path):
    item = attach(app, entry(app), [source_image(tmp_path)])
    stored = app.data_dir / 'expenses' / item['attachments'][0]['relative_path']
    stored.write_bytes(stored.read_bytes() + b'x')
    with pytest.raises(ValueError):
        app.jobs.runners['expenses.attachments.merge'](Job({'id': item['id']}))
