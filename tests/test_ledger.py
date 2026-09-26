"""Causal replication invariants using isolated devices and a fake WebDAV server."""
import copy
import csv
import hashlib
import json
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.ledger import Ledger, DEFAULT_PROJECT
from toolbox.ledger_sync import LedgerRemote
from toolbox.settings import atomic_json


def item(store, entity_id=None, **changes):
    return store.patch('entry', entity_id or uuid.uuid4().hex,
                       {'title': 'fixture', 'date': '2026-09-10', 'amount': '12.30', 'amount_minor': 1230,
                        'currency': 'CNY', 'entry_type': 'expense', 'status': 'waiting', 'category': '日常',
                        'notes': '', 'project_id': DEFAULT_PROJECT, 'created_at': 1, **changes})


def exchange(a, b):
    a.ingest(b.export_operations())
    b.ingest(a.export_operations())


def test_disjoint_edits_merge_same_field_conflicts_resolve_and_delete_wins(tmp_path):
    a, b = Ledger(tmp_path / 'a'), Ledger(tmp_path / 'b')
    entry = item(a)
    exchange(a, b)
    a.patch('entry', entry['id'], {'title': 'desktop'})
    b.patch('entry', entry['id'], {'notes': 'phone'})
    exchange(a, b)
    assert a.get('entry', entry['id']) == b.get('entry', entry['id'])
    assert a.get('entry', entry['id'])['title'] == 'desktop'
    assert a.get('entry', entry['id'])['notes'] == 'phone'
    assert not a.conflicts()
    a.patch('entry', entry['id'], {'amount': '15.00', 'amount_minor': 1500})
    b.patch('entry', entry['id'], {'amount': '16.00', 'amount_minor': 1600})
    exchange(a, b)
    conflict = a.conflicts()[0]
    assert set(conflict['fields']) == {'amount'}
    assert {c['value'] for c in conflict['fields']['amount']} == {'15.00', '16.00'}
    a.patch('entry', entry['id'], {'amount': '15.00', 'amount_minor': 1500}, parents=conflict['heads'])
    exchange(a, b)
    assert not b.conflicts()
    assert b.get('entry', entry['id'])['amount_minor'] == 1500
    a.patch('entry', entry['id'], {}, deleted=True)
    b.patch('entry', entry['id'], {'title': 'offline stale edit'})
    exchange(a, b)
    assert not a.list_entities('entry') and not b.list_entities('entry')
    with pytest.raises(ValueError, match='已删除'):
        b.patch('entry', entry['id'], {'title': 'resurrection'})
    assert Ledger(tmp_path / 'b').get('entry', entry['id'])['deleted']


def test_missing_parents_durable_then_order_independent_and_reject_late_cross_entity(tmp_path):
    a, b = Ledger(tmp_path / 'a'), Ledger(tmp_path / 'b')
    first = item(a)
    second = a.patch('entry', first['id'], {'title': 'changed'})
    ops = a.export_operations()
    b.ingest([ops[-1]])
    assert b.incomplete() == 1 and b.get('entry', first['id']) is None
    b = Ledger(tmp_path / 'b')
    b.ingest([ops[0]])
    assert b.get('entry', first['id']) == second
    c = Ledger(tmp_path / 'c')
    c.ingest([ops[-1]])
    wrong = copy.deepcopy(ops[0])
    wrong['entity_id'] = uuid.uuid4().hex
    with pytest.raises(ValueError, match='不同条目'):
        c.ingest([wrong])
    assert len(c.export_operations()) == 1


def test_cycles_duplicate_ids_and_invalid_remote_values_rejected_before_disk(tmp_path):
    a = Ledger(tmp_path / 'a')
    base = {'schema': 1, 'op_id': uuid.uuid4().hex, 'entity_type': 'project', 'entity_id': uuid.uuid4().hex,
            'parents': [], 'changes': {'name': 'fixture'}, 'deleted': False, 'created_at': 1}
    other = {**base, 'op_id': uuid.uuid4().hex, 'parents': [base['op_id']]}
    cyclic = {**base, 'parents': [other['op_id']]}
    with pytest.raises(ValueError, match='循环'):
        a.ingest([cyclic, other])
    with pytest.raises(ValueError, match='重复'):
        a.ingest([base, {**base, 'changes': {'name': 'different'}}])
    for changes in ({'name': []}, {'archived': 'false'}, {'settlement_mode': 'bad'}):
        with pytest.raises(ValueError):
            a.ingest([{**base, 'changes': changes}])
    assert not list(a.ops_dir.glob('*.json'))


def test_income_status_and_amount_preview_are_coherent(tmp_path):
    a, b = Ledger(tmp_path / 'a'), Ledger(tmp_path / 'b')
    entry = item(a)
    exchange(a, b)
    a.patch('entry', entry['id'], {'entry_type': 'income', 'status': 'not_applicable'})
    b.patch('entry', entry['id'], {'status': 'reimbursed'})
    exchange(a, b)
    value = a.get('entry', entry['id'])
    assert value['entry_type'] == 'income' and value['status'] == 'not_applicable'
    assert 'status' in value['conflicts']


def test_attachment_remove_wins_concurrent_metadata_edit(tmp_path):
    a, b = Ledger(tmp_path / 'a'), Ledger(tmp_path / 'b')
    entry = item(a)
    aid, body = uuid.uuid4().hex, b'%PDF-fixture'
    attachment = {'id': aid, 'original_name': 'fixture.pdf', 'kind': 'receipt', 'size': len(body),
                  'sha256': a.put_blob(body), 'relative_path': f'attachments/{entry["id"]}/{aid}.pdf', 'created_at': 1}
    a.patch('entry', entry['id'], {'attachment:' + aid: attachment})
    exchange(a, b)
    a.patch('entry', entry['id'], {'attachment:' + aid: None})
    b.patch('entry', entry['id'], {'attachment:' + aid: {**attachment, 'original_name': 'edited.pdf'}})
    exchange(a, b)
    assert a.get('entry', entry['id'])['attachment:' + aid] is None
    assert b.get('entry', entry['id'])['attachment:' + aid] is None
    assert a.conflicts()  # Retained metadata can be explicitly recovered if desired.


def test_concurrent_zero_amount_and_income_type_does_not_break_listing(tmp_path):
    app = App(tmp_path / 'data', register_live=False)
    try:
        entry = app.call('expenses.save', {'title': 'fixture', 'date': '2026-09-10', 'amount': '1'})
        offline = Ledger(tmp_path / 'offline')
        offline.ingest(app.ledger.export_operations())
        app.call('expenses.save', {'id': entry['id'], 'entry_type': 'income'})
        offline.patch('entry', entry['id'], {'amount': '0.00', 'amount_minor': 0})
        app.ledger.apply_remote(offline.export_operations())
        rows = app.call('expenses.list', {'month': '2026-09'})['items']
        assert len(rows) == 1 and rows[0]['amount'] == '0.00'
        assert rows[0]['entry_type'] == 'income' and rows[0]['validation_warning']
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)


class Job:
    def check_cancelled(self): pass
    def progress(self, *_): pass


class RemoteFixture(LedgerRemote):
    def __init__(self, server):
        self.server = server
        self.directory = 'https://fixture.invalid/WinToolbox/'
        self.fail_after_put = False
    def ensure_directory(self): pass
    def mkdir(self, url): pass
    def children(self, url):
        return [(key[len(url):], False) for key in sorted(self.server) if key.startswith(url) and '/' not in key[len(url):]]
    def read(self, url, limit=1024 * 1024):
        body = self.server[url]
        if len(body) > limit: raise ValueError('too large')
        return body
    def request(self, method, url, **kwargs):
        assert method == 'PUT' and kwargs['headers']['If-None-Match'] == '*'
        code = 412 if url in self.server else 201
        if code == 201:
            self.server[url] = kwargs['content']
        if self.fail_after_put:
            self.fail_after_put = False
            raise ConnectionError('fixture lost ACK')
        return type('Response', (), {'status_code': code})()


def test_sync_retry_offline_queue_attachments_and_corrupt_blob(tmp_path):
    a, b = Ledger(tmp_path / 'a'), Ledger(tmp_path / 'b')
    entry = item(a)
    body = b'%PDF-1.7 fixture'
    digest, aid = a.put_blob(body), uuid.uuid4().hex
    attachment = {'id': aid, 'original_name': 'fixture.pdf', 'kind': 'receipt', 'size': len(body), 'sha256': digest,
                  'relative_path': f'attachments/{entry["id"]}/{aid}.pdf', 'created_at': 1}
    a.patch('entry', entry['id'], {'attachment:' + aid: attachment})
    server = {}
    remote = RemoteFixture(server)
    remote.fail_after_put = True
    with pytest.raises(ConnectionError):
        remote.sync(a, Job())
    assert len(Ledger(tmp_path / 'a').export_operations()) == 2
    remote.sync(a, Job())
    assert remote.sync(a, Job())['uploaded'] == 0
    remote.sync(b, Job())
    assert b.get_blob(digest) == body
    assert b.get('entry', entry['id']) == a.get('entry', entry['id'])
    c = Ledger(tmp_path / 'c')
    server[remote.directory + 'ledger-v1/blobs/' + digest] = b'corrupt'
    with pytest.raises(ValueError, match='哈希'):
        remote.sync(c, Job())
    assert not any('attachment:' + aid in op['changes'] for op in c.export_operations())


def finish(app, job):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = app.jobs.get(job['id'])
        if value['status'] not in ('running', 'queued', 'cancelling'):
            assert value['status'] == 'completed', value
            return value['result']
        time.sleep(.01)
    raise AssertionError('job timed out')


def test_projects_inclusive_date_export_formula_safety_and_archive(tmp_path):
    app = App(tmp_path / 'data', register_live=False)
    try:
        project = app.call('expenses.projects.save', {'name': '出差'})
        for day in ('2026-09-09', '2026-09-10', '2026-09-11'):
            app.call('expenses.save', {'title': '=HYPERLINK("bad")', 'date': day, 'amount': '0.10', 'project_id': project['id']})
        params = {'project_id': project['id'], 'start_date': '2026-09-10', 'end_date': '2026-09-10'}
        selected = app.call('expenses.list', params)
        assert len(selected['items']) == 1 and selected['settlement_mode'] == 'general'
        assert selected['summary'][0]['share_due_minor'] == 0
        result = finish(app, app.call('expenses.export', {**params, 'format': 'csv'}))
        with Path(result['path']).open(encoding='utf-8-sig', newline='') as source:
            rows = list(csv.reader(source))
        assert len(rows) == 2 and rows[1][3].startswith("'=")
        result = finish(app, app.call('expenses.export', {**params, 'format': 'xlsx'}))
        with zipfile.ZipFile(result['path']) as archive:
            sheet = archive.read('xl/worksheets/sheet1.xml').decode()
            assert '<f>' not in sheet and 'HYPERLINK' in sheet and '0.10' in sheet
            assert '<c r="G2" s="2"><v>0.10</v></c>' in sheet
            assert '<c r="B2" s="3"><v>' in sheet and '<autoFilter' in sheet and 'state="frozen"' in sheet
        app.call('expenses.projects.save', {'id': project['id'], 'archived': True})
        assert len(app.call('expenses.list', params)['items']) == 1
        with pytest.raises(ValueError, match='未归档'):
            app.call('expenses.save', {'title': 'new', 'date': '2026-09-10', 'amount': '1', 'project_id': project['id']})
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)


def test_legacy_migration_once_and_durable_edit_cache_recovery(tmp_path):
    directory = tmp_path / 'data'
    entity_id = uuid.uuid4().hex
    legacy = {'id': entity_id, 'revision': 7, 'title': 'legacy', 'date': '2026-09-10', 'amount': '1.01',
              'amount_minor': 101, 'currency': 'CNY', 'status': 'waiting', 'category': 'AI订阅', 'notes': '',
              'created_at': 1, 'updated_at': 1, 'attachments': []}
    path = directory / 'expenses' / 'items' / (entity_id + '.json')
    atomic_json(path, legacy)
    app = App(directory, register_live=False)
    try:
        value = app.call('expenses.get', {'id': entity_id})
        assert value['project_id'] == DEFAULT_PROJECT and value['revision'] >= 7
        assert len(app.ledger.export_operations()) == 1
        assert app.call('expenses.list', {'month': '2026-09'})['summary'][0]['share_due_minor'] == 51
        # Simulate a durable op whose materialized row was not written before exit.
        app.ledger.patch('entry', entity_id, {'notes': 'durable offline edit'})
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)
    app = App(directory, register_live=False)
    try:
        assert app.call('expenses.get', {'id': entity_id})['notes'] == 'durable offline edit'
        assert len(app.ledger.export_operations()) == 2
        status = app.call('expenses.sync.status')
        assert not status['configured'] and status['pending'] == 2
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)


def test_auto_sync_start_change_and_offline_retry_status(tmp_path, monkeypatch):
    calls = []
    class AutomaticRemote:
        def __init__(self, config): pass
        def close(self): pass
        def sync(self, store, job):
            calls.append(len(store.export_operations()))
            if len(calls) == 1:
                raise ConnectionError('fixture offline')
            return {'uploaded': len(store.export_operations()), 'downloaded': 0, 'conflicts': 0,
                    'synced_operations': len(store.export_operations())}
    monkeypatch.setattr('toolbox.ledger_service.LedgerRemote', AutomaticRemote)
    directory = tmp_path / 'data'
    directory.mkdir()
    atomic_json(directory / 'webdav.json', {'url': 'https://fixture.invalid/', 'username': 'fixture', 'password_dpapi': 'fake'})
    app = App(directory, register_live=False)
    try:
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline: time.sleep(.01)
        assert calls == [0]
        deadline = time.monotonic() + 5
        while app.call('expenses.sync.status')['syncing'] and time.monotonic() < deadline: time.sleep(.01)
        assert 'offline' in app.call('expenses.sync.status')['error']
        app.call('expenses.save', {'title': 'offline receipt', 'date': '2026-09-10', 'amount': '1'})
        deadline = time.monotonic() + 5
        while (len(calls) < 2 or app.call('expenses.sync.status')['syncing']) and time.monotonic() < deadline: time.sleep(.01)
        assert calls[-1] == 1
        assert app.call('expenses.sync.status')['pending'] == 0
        assert app.call('expenses.sync.status')['last_sync']
    finally:
        app.close()
        app.jobs.pool.shutdown(wait=True)
