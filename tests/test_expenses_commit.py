import copy
from pathlib import Path
import time
import uuid

import pytest

from toolbox.app import App
from toolbox.jobs import Cancelled


class Job:
    def __init__(self, params, check=None):
        self.params = copy.deepcopy(params)
        self.check = check or (lambda: None)
        self.committed = False

    def check_cancelled(self):
        if not self.committed:
            self.check()

    def mark_committed(self):
        self.committed = True


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / 'data', register_live=False)
    instance.data_sync.stop_worker()
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def pdf(tmp_path, name='receipt.pdf'):
    path = tmp_path / name
    path.write_bytes(b'%PDF-1.4\n% fixture receipt\n%%EOF\n')
    return path


def draft(**extra):
    return {'title': 'fixture entry', 'date': '2026-09-27', 'amount': '12.34',
            'attachments_add': [], 'attachments_remove': [], 'request_id': str(uuid.uuid4()), **extra}


def commit(app, params, check=None):
    return app.jobs.runners['expenses.commit'](Job(params, check))['entry']


def assert_empty(app):
    assert app.call('expenses.list', {'month': '2026-09'})['items'] == []
    assert app.ledger.list_entities('entry') == []
    assert not list((app.data_dir / 'expenses' / 'attachments').rglob('*.pdf'))


def test_rpc_new_entry_and_attachments_publish_in_one_operation(app, tmp_path):
    source = pdf(tmp_path)
    params = draft(attachments_add=[{'path': str(source), 'kind': 'receipt'}])
    record = app.call('expenses.commit', params)
    deadline = time.monotonic() + 4
    while record['id'] in app.jobs.active and time.monotonic() < deadline:
        time.sleep(.01)
    finished = app.jobs.get(record['id'])
    assert finished['status'] == 'completed', finished
    entry = finished['result']['entry']
    operations = app.ledger.export_operations()
    assert len(operations) == 1
    assert operations[0]['changes']['title'] == params['title']
    assert sum(key.startswith('attachment:') for key in operations[0]['changes']) == 1
    attached = entry['attachments'][0]
    assert (app.data_dir / 'expenses' / attached['relative_path']).read_bytes() == source.read_bytes()
    assert app.ledger.get_blob(attached['sha256']) == source.read_bytes()


def test_invalid_attachment_keeps_entire_new_entry_unpublished(app, tmp_path):
    good = pdf(tmp_path)
    bad = tmp_path / 'bad.pdf'
    bad.write_text('not a PDF')
    with pytest.raises(ValueError, match='内容'):
        commit(app, draft(attachments_add=[{'path': str(good), 'kind': 'receipt'}, {'path': str(bad), 'kind': 'invoice'}]))
    assert_empty(app)


def test_cancel_after_staging_before_publish_cleans_owned_files(app, tmp_path):
    source = pdf(tmp_path)
    def check():
        if list((app.data_dir / 'expenses' / 'attachments').rglob('*.pdf')):
            raise Cancelled()
    with pytest.raises(Cancelled):
        commit(app, draft(attachments_add=[{'path': str(source), 'kind': 'receipt'}]), check)
    assert_empty(app)
    assert source.exists()


def test_edit_add_remove_atomic_and_invalid_replacement_preserves_original(app, tmp_path):
    source = pdf(tmp_path)
    original = commit(app, draft(attachments_add=[{'path': str(source), 'kind': 'receipt'}]))
    attachment = original['attachments'][0]
    params = draft(id=original['id'], revision=original['revision'], title='updated',
                   attachments_remove=[attachment['id']], attachments_add=[{'path': str(tmp_path / 'missing.pdf'), 'kind': 'invoice'}])
    with pytest.raises(ValueError):
        commit(app, params)
    assert app.call('expenses.get', {'id': original['id']}) == original
    params['request_id'] = str(uuid.uuid4())
    params['attachments_add'][0]['path'] = str(source)
    edited = commit(app, params)
    assert edited['title'] == 'updated'
    assert len(edited['attachments']) == 1 and edited['attachments'][0]['id'] != attachment['id']
    operations = app.ledger.export_operations()
    assert len(operations) == 2
    change = next(op['changes'] for op in operations if op['parents'])
    assert change['attachment:' + attachment['id']] is None
    assert change['title'] == 'updated'
    assert any(key.startswith('attachment:') and value is not None for key, value in change.items())
    assert (app.data_dir / 'expenses' / attachment['relative_path']).exists()


def test_stale_revision_and_edit_during_staging_are_rejected(app, tmp_path, monkeypatch):
    from toolbox.features import expenses
    original = commit(app, draft())
    source = pdf(tmp_path)
    params = draft(id=original['id'], revision=original['revision'], attachments_add=[{'path': str(source), 'kind': 'receipt'}])
    verify = expenses.verify_type
    changed = []
    def edit_while_copying(path, suffix):
        verify(path, suffix)
        if not changed:
            changed.append(True)
            app.call('expenses.save', {'id': original['id'], 'revision': original['revision'], 'title': 'concurrent edit'})
    monkeypatch.setattr(expenses, 'verify_type', edit_while_copying)
    with pytest.raises(ValueError, match='更新'):
        commit(app, params)
    with pytest.raises(ValueError, match='更新'):
        commit(app, {**params, 'request_id': str(uuid.uuid4())})
    current = app.call('expenses.get', {'id': original['id']})
    assert current['title'] == 'concurrent edit' and not current['attachments']


def test_same_request_retry_is_idempotent_and_changed_payload_rejected(app, tmp_path):
    source = pdf(tmp_path)
    params = draft(attachments_add=[{'path': str(source), 'kind': 'invoice'}])
    original = commit(app, params)
    source.unlink()
    again = commit(app, params)
    assert again['id'] == original['id'] and again['attachments'] == original['attachments']
    assert len(app.ledger.export_operations()) == 1
    with pytest.raises(ValueError, match='请求标识'):
        commit(app, {**params, 'title': 'different payload'})


def test_no_change_commit_has_durable_idempotency_marker(app):
    original = commit(app, draft())
    params = draft(id=original['id'], revision=original['revision'])
    saved = commit(app, params)
    count = len(app.ledger.export_operations())
    assert count == 2
    assert commit(app, params)['id'] == saved['id']
    assert len(app.ledger.export_operations()) == count


@pytest.mark.parametrize('failure', ['projection', 'after_operation_write'])
def test_published_operation_survives_failure_and_retry_recovers(app, tmp_path, monkeypatch, failure):
    import toolbox.ledger as ledger_module
    from toolbox.features import expenses
    source = pdf(tmp_path)
    params = draft(attachments_add=[{'path': str(source), 'kind': 'receipt'}])
    module = expenses if failure == 'projection' else ledger_module
    write = module.atomic_json
    failed = []
    def failing_write(path, value):
        wanted = Path(path).parent.name == ('items' if failure == 'projection' else 'ops')
        if wanted and not failed:
            failed.append(True)
            if failure == 'after_operation_write':
                write(path, value)
            raise OSError('fixture write interrupted')
        write(path, value)
    monkeypatch.setattr(module, 'atomic_json', failing_write)
    with pytest.raises(OSError, match='interrupted'):
        commit(app, params)
    assert len(app.ledger.export_operations()) == 1
    assert len(list((app.data_dir / 'expenses' / 'attachments').rglob('*.pdf'))) == 1
    source.unlink()
    result = commit(app, params)
    assert result['title'] == params['title'] and len(result['attachments']) == 1
    assert len(app.ledger.export_operations()) == 1


def test_operation_write_failure_before_publish_cleans_entry_files(app, tmp_path, monkeypatch):
    import toolbox.ledger as ledger_module
    source = pdf(tmp_path)
    def fail(*_):
        raise OSError('fixture not published')
    monkeypatch.setattr(ledger_module, 'atomic_json', fail)
    with pytest.raises(OSError):
        commit(app, draft(attachments_add=[{'path': str(source), 'kind': 'receipt'}]))
    assert_empty(app)


def test_failed_projection_cannot_let_later_request_overwrite_unseen_update(app, monkeypatch):
    from toolbox.features import expenses
    original = commit(app, draft())
    write = expenses.atomic_json
    failed = []
    def fail_projection(path, value):
        if Path(path).parent.name == 'items' and not failed:
            failed.append(True)
            raise OSError('fixture projection failure')
        write(path, value)
    monkeypatch.setattr(expenses, 'atomic_json', fail_projection)
    with pytest.raises(OSError):
        commit(app, draft(id=original['id'], revision=original['revision'], title='already published'))
    with pytest.raises(ValueError, match='更新'):
        commit(app, draft(id=original['id'], revision=original['revision'], title='stale editor'))
    assert app.call('expenses.get', {'id': original['id']})['title'] == 'already published'
