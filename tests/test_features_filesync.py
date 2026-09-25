import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from toolbox.features import filesync as fs
from toolbox.storage import Storage


class Job:
    def __init__(self, **params):
        self.params = params
    def check_cancelled(self):
        pass
    def progress(self, *args):
        pass
    def mark_committed(self):
        pass


@pytest.fixture
def service(tmp_path):
    app = SimpleNamespace(storage=Storage(tmp_path / 'data'), data_lock=threading.RLock(), maintenance=False)
    return fs.FileSync(app)


def rule(service, tmp_path, **options):
    source, target = tmp_path / 'source', tmp_path / 'target'
    if options.get('kind') == 'folder':
        source.mkdir()
        target.mkdir()
    else:
        source.write_text('first', encoding='utf-8')
    return service.save({'source': str(source), 'target': str(target), **options})


def sync(service, r, **extra):
    plan = service.run(Job(id=r['id']), preview=True)
    return service.run(Job(id=r['id'], token=plan['token'], **extra))


def test_same_size_same_time_changes_are_not_skipped(service, tmp_path):
    r = rule(service, tmp_path)
    sync(service, r)
    source, target = Path(r['source']), Path(r['target'])
    stamp = source.stat().st_mtime_ns
    source.write_text('other', encoding='utf-8')
    os.utime(source, ns=(stamp, stamp))
    result = sync(service, r)
    assert result['changed'] == 1
    assert target.read_text() == 'other'
    assert (Path(result['backups'][0]) / 'content').read_text() == 'first'


def test_bidirectional_baseline_beats_timestamps_and_survives_restart(service, tmp_path):
    r = rule(service, tmp_path, bidirectional=True)
    sync(service, r)
    target = Path(r['target'])
    target.write_text('edited')
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    service = fs.FileSync(service.app)
    sync(service, r)
    assert Path(r['source']).read_text() == 'edited'


def test_both_sides_changed_conflict_requires_resolution(service, tmp_path):
    r = rule(service, tmp_path, bidirectional=True)
    sync(service, r)
    Path(r['source']).write_text('left')
    Path(r['target']).write_text('right')
    with pytest.raises(ValueError, match='冲突'):
        sync(service, r)
    assert Path(r['target']).read_text() == 'right'
    sync(service, r, resolutions={'': 'source'})
    assert Path(r['target']).read_text() == 'left'


def test_stale_preview_does_not_write(service, tmp_path):
    r = rule(service, tmp_path)
    plan = service.run(Job(id=r['id']), preview=True)
    Path(r['source']).write_text('changed since preview')
    with pytest.raises(ValueError, match='重新预览'):
        service.run(Job(id=r['id'], token=plan['token']))
    assert not Path(r['target']).exists()


def test_replace_failure_preserves_original(service, tmp_path, monkeypatch):
    r = rule(service, tmp_path)
    sync(service, r)
    Path(r['source']).write_text('replacement')
    original = os.replace
    def fail(source, target):
        if Path(target) == Path(r['target']):
            raise PermissionError('locked destination')
        return original(source, target)
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(PermissionError):
        sync(service, r)
    assert Path(r['target']).read_text() == 'first'
    assert not list(tmp_path.glob('*.filesync.tmp'))


def test_delete_vs_edit_conflicts_and_delete_is_backed_up(service, tmp_path):
    r = rule(service, tmp_path, bidirectional=True, deletes=True)
    sync(service, r)
    Path(r['source']).unlink()
    Path(r['target']).write_text('unsynced edit')
    with pytest.raises(ValueError, match='冲突'):
        sync(service, r)
    result = sync(service, r, resolutions={'': 'source'})
    assert not Path(r['target']).exists()
    assert (Path(result['backups'][0]) / 'content').read_text() == 'unsynced edit'
    assert service.get(r['id'])['baseline'] == {}


def test_delete_propagation_and_no_delete_mode(service, tmp_path):
    r = rule(service, tmp_path, bidirectional=True)
    sync(service, r)
    Path(r['source']).unlink()
    sync(service, r)
    assert Path(r['source']).read_text() == 'first'
    r = service.save({**r, 'deletes': True})
    Path(r['source']).unlink()
    sync(service, r)
    assert not Path(r['target']).exists()


def test_folder_filters_protect_excluded_files_and_shared_backups(service, tmp_path):
    r = rule(service, tmp_path, kind='folder', deletes=True, include=['**/*.md'], exclude=['private/**'])
    source, target = Path(r['source']), Path(r['target'])
    (source / 'readme.md').write_text('notes')
    (target / 'extra.md').write_text('old')
    (target / 'keep.txt').write_text('untouched')
    (target / 'private').mkdir()
    (target / 'private/secret.md').write_text('private')
    (target / '.back').mkdir()
    (target / '.back/old.md').write_text('old backup')
    sync(service, r)
    assert (target / 'readme.md').read_text() == 'notes'
    assert not (target / 'extra.md').exists()
    assert (target / 'keep.txt').exists()
    assert (target / 'private/secret.md').exists()
    assert (target / '.back/old.md').exists()


def test_missing_folder_root_never_propagates_mass_deletion(service, tmp_path):
    r = rule(service, tmp_path, kind='folder', bidirectional=True, deletes=True)
    (Path(r['source']) / 'doc').write_text('keep')
    sync(service, r)
    Path(r['source']).rename(tmp_path / 'disconnected')
    with pytest.raises(ValueError, match='根目录不可用'):
        sync(service, r)
    assert (Path(r['target']) / 'doc').read_text() == 'keep'


def test_overlapping_rules_and_same_file_rejected(service, tmp_path):
    r = rule(service, tmp_path)
    with pytest.raises(ValueError, match='重叠'):
        service.save({'source': r['source'], 'target': str(tmp_path / 'another')})
    alias = tmp_path / 'hardlink'
    os.link(r['source'], alias)
    with pytest.raises(ValueError, match='同一个文件'):
        fs.validate({'source': r['source'], 'target': str(alias)})
    with pytest.raises(ValueError, match='包含'):
        fs.validate({'source': str(tmp_path), 'target': str(tmp_path / 'child'), 'kind': 'folder'})


def test_import_is_read_only_disabled_idempotent_and_rebuilds_baseline(service, tmp_path):
    source = tmp_path / 'source'; source.write_text('real')
    legacy = tmp_path / 'store.json'
    legacy.write_text(json.dumps({'rules': [{'name': 'old', 'sourcePath': str(source),
        'targetPath': str(tmp_path / 'target'), 'autoSync': True, 'bidirectional': True,
        'syncState': {'mirroredEntries': ['']}}]}), encoding='utf-8')
    before = legacy.read_bytes()
    result = service.import_legacy(Job(path=str(legacy)))
    assert len(result['imported']) == 1
    imported = result['imported'][0]
    assert not imported['auto']
    assert service.get(imported['id'])['baseline'] == {}
    assert not service.import_legacy(Job(path=str(legacy)))['imported']
    assert legacy.read_bytes() == before
    assert source.read_text() == 'real'


def test_retention_uses_backup_creation_not_original_mtime(service, tmp_path):
    r = rule(service, tmp_path, retention=1)
    sync(service, r)
    os.utime(r['target'], ns=(1_000_000_000, 1_000_000_000))
    Path(r['source']).write_text('next')
    result = sync(service, r)
    root = Path(result['backups'][0])
    assert (root / 'content').exists()
    value = json.loads((root / 'manifest.json').read_text())
    value['created'] = time.time() - 2 * 86400
    (root / 'manifest.json').write_text(json.dumps(value))
    fs.purge_backups(service.get(r['id']), lambda: None)
    assert not root.exists()


def test_source_changes_during_copy_do_not_overwrite(service, tmp_path, monkeypatch):
    r = rule(service, tmp_path)
    sync(service, r)
    Path(r['source']).write_text('next')
    original = fs.guarded_copy
    def changed(source, target, a, b, check):
        source.write_text('changed again')
        return original(source, target, a, b, check)
    monkeypatch.setattr(fs, 'guarded_copy', changed)
    with pytest.raises(ValueError):
        sync(service, r)
    assert Path(r['target']).read_text() == 'first'


def test_edit_gate_fails_fast(service, tmp_path):
    with service.execution:
        with pytest.raises(ValueError, match='正在进行'):
            rule(service, tmp_path)


def test_log_recovery_deduplicates_renamed_rules_and_disables_deletes(service, tmp_path):
    source = tmp_path / 'notes'; source.mkdir()
    target = tmp_path / 'docs'
    log = tmp_path / 'app.log.jsonl'
    log.write_text('\n'.join(json.dumps({'message': f'开始执行规则“{name}”，触发方式：Poll，源：{source}，目标：{target}'}, ensure_ascii=False) for name in ('旧名', '新名')), encoding='utf-8')
    result = service.import_legacy(Job(path=str(log)))
    assert result['recovered']
    assert len(result['imported']) == 1
    r = result['imported'][0]
    assert r['name'] == '新名' and r['kind'] == 'folder'
    assert r['bidirectional'] and not r['auto'] and not r['deletes']
    assert not target.exists()


def test_symlink_rejected(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    link = tmp_path / 'link'
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip('创建符号链接需要 Windows 开发者模式')
    with pytest.raises(ValueError, match='链接'):
        fs.validate({'source': str(link), 'target': str(tmp_path / 'target'), 'kind': 'folder'})


def test_job_rpc_and_automatic_loop_use_real_background_jobs(tmp_path):
    from toolbox.app import App
    app = App(tmp_path / 'app', register_features=False, register_live=False)
    try:
        fs.register(app)
        r = rule(app.filesync, tmp_path, auto=True)
        app.filesync.tick()
        identifier = app.filesync.pending[r['id']]
        deadline = time.monotonic() + 5
        while app.jobs.get(identifier)['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.02)
        assert app.jobs.get(identifier)['status'] == 'completed'
        assert Path(r['target']).read_text() == 'first'
        app.after_restore()
        assert not app.filesync.get(r['id'])['auto']
    finally:
        app.close()


def test_cancelled_batch_keeps_completed_baseline(service, tmp_path):
    from toolbox.jobs import Cancelled
    r = rule(service, tmp_path, kind='folder')
    for name in ('a', 'b'):
        (Path(r['source']) / name).write_text(name)
    plan = service.run(Job(id=r['id']), preview=True)
    class CancelAfterFirst(Job):
        def progress(self, number, *args):
            if number > 0:
                raise Cancelled()
    with pytest.raises(Cancelled):
        service.run(CancelAfterFirst(id=r['id'], token=plan['token']))
    assert 'a' in service.get(r['id'])['baseline']
    assert (Path(r['target']) / 'a').read_text() == 'a'
    sync(service, r)
    assert (Path(r['target']) / 'b').read_text() == 'b'
