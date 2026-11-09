"""Cross-library migration: staged reads, complete backups, merge, replace and recovery."""
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from toolbox.project_memory.store import MemoryStore
from toolbox.project_memory_reconcile import reconcile
from test_features_project_memory_sync import dav, Job


def test_cross_library_merge_keeps_records_conflicts_blobs_and_bindings(tmp_path, dav):
    remote, state = dav
    a = MemoryStore(tmp_path / 'a', device_id='a')
    b = MemoryStore(tmp_path / 'b', device_id='b')
    try:
        remote_project = a.create_project('远端项目')
        blob = a.add_blob(b'remote attachment')
        a.save_entry(dict(id='remote-entry', title='远端正文', kind='knowledge', project_id=remote_project['id'], attachments=[blob]))
        a.save_entry(dict(id='common', title='远端版本', kind='knowledge', scope='global'))
        remote.sync(a, Job())
        local_project = b.create_project('本机项目')
        folder = tmp_path / 'project'; folder.mkdir()
        b.bind_project(local_project['id'], folder)
        b.save_entry(dict(id='local-entry', title='本机正文', kind='knowledge', project_id=local_project['id']))
        b.save_entry(dict(id='common', title='本机版本', kind='knowledge', scope='global'))
        old_id = b.library_id
        before = dict(state['files'])
        result = reconcile(b, remote, a.library_id, 'merge', Job(), threading.RLock())
        assert state['files'] == before  # Preflight never publishes or mutates the selected remote.
        assert b.library_id == a.library_id
        assert b.get_entry('local-entry')['title'] == '本机正文'
        assert b.get_entry('remote-entry')['title'] == '远端正文'
        assert b.get_entry('common')['conflict']
        assert b.get_blob(blob['hash']) == b'remote attachment'
        assert json.loads((folder / '.agent/toolbox.json').read_text())['library_id'] == a.library_id
        with sqlite3.connect(Path(result['recovery_path']) / 'memory.sqlite3') as db:
            assert db.execute("SELECT value FROM metadata WHERE key='library_id'").fetchone()[0] == old_id
        remote.sync(b, Job())
        remote.sync(a, Job())
        a.subscribe(local_project['id'])
        remote.sync(a, Job())
        assert a.get_entry('local-entry')['title'] == '本机正文'
    finally:
        a.close(); b.close()


def test_remote_replace_removes_local_only_records_but_preserves_backup(tmp_path, dav):
    remote, state = dav
    a = MemoryStore(tmp_path / 'a', device_id='a')
    b = MemoryStore(tmp_path / 'b', device_id='b')
    try:
        a.save_entry(dict(id='remote', title='远端配置', kind='knowledge', scope='global'))
        b.save_entry(dict(id='local', title='本机配置', kind='knowledge', scope='global'))
        remote.sync(a, Job())
        original = b.export_operations()
        result = reconcile(b, remote, a.library_id, 'remote', Job(), threading.RLock())
        assert b.get_entry('remote')['title'] == '远端配置'
        with pytest.raises(KeyError): b.get_entry('local')
        backup = MemoryStore(Path(result['recovery_path']), device_id='b')
        try:
            assert backup.export_operations() == original
            assert backup.get_entry('local')['title'] == '本机配置'
        finally: backup.close()
    finally:
        a.close(); b.close()


def test_invalid_or_missing_remote_keeps_local_untouched(tmp_path, dav):
    remote, state = dav
    a = MemoryStore(tmp_path / 'a', device_id='a')
    b = MemoryStore(tmp_path / 'b', device_id='b')
    try:
        a.save_entry(dict(id='remote', title='远端配置', kind='knowledge', scope='global'))
        b.save_entry(dict(id='local', title='本机配置', kind='knowledge', scope='global'))
        remote.sync(a, Job())
        original, library = b.export_operations(), b.library_id
        with pytest.raises(ValueError, match='不存在'):
            reconcile(b, remote, 'missing-library', 'remote', Job(), threading.RLock())
        path = next(path for path in state['files'] if '/global/' in path)
        state['files'][path] = b'{}'
        with pytest.raises(ValueError):
            reconcile(b, remote, a.library_id, 'remote', Job(), threading.RLock())
        assert b.export_operations() == original and b.library_id == library
    finally:
        a.close(); b.close()
