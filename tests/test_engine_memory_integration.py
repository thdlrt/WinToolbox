"""Real App / jobs / backup integration; every byte stays in temporary fixtures."""
import json
import time
import zipfile
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.project_memory import store as memory_store


def finish(app, job):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        row = app.jobs.get(job['id'])
        if row['status'] not in ('queued', 'running', 'cancelling') and job['id'] not in app.jobs.active:
            assert row['status'] == 'completed', row
            return row
        time.sleep(.02)
    raise AssertionError('Project memory integration job timed out')


@pytest.fixture
def apps(tmp_path, monkeypatch):
    # A real App registers all features. Machine identity must never touch user state.
    monkeypatch.setattr(memory_store, 'state_directory', lambda: tmp_path / 'machine-a')
    original = App(tmp_path / 'original', register_live=False)
    monkeypatch.setattr(memory_store, 'state_directory', lambda: tmp_path / 'machine-b')
    target = App(tmp_path / 'target', register_live=False)
    try:
        yield original, target
    finally:
        original.close()
        target.close()
        original.jobs.pool.shutdown(wait=True)
        target.jobs.pool.shutdown(wait=True)


def create(app, tmp_path):
    project = app.call('memory.project.create', {'name': '集成测试项目'})
    path = tmp_path / 'project-source'
    path.mkdir()
    app.call('memory.project.bind', {'project_id': project['id'], 'path': str(path)})
    attachment = tmp_path / '证据.txt'
    attachment.write_text('已验证原始证据', encoding='utf-8')
    blob = app.call('memory.attachment.add', {'path': str(attachment)})
    entry = app.call('memory.entry.save', {'entry': {
        'kind': 'knowledge', 'scope': 'project', 'project_id': project['id'],
        'title': '已验证方法', 'body': '知识正文', 'knowledge_type': '经验', 'attachments': [blob]}, 'parents': []})
    return project, entry, blob


def test_registered_app_crud_and_completion(apps, tmp_path):
    app, _ = apps
    project, entry, _ = create(app, tmp_path)
    updated = app.call('memory.entry.save', {'entry': {**entry, 'body': '修改后的知识正文'}, 'parents': entry['heads']})
    assert updated['body'] == '修改后的知识正文'
    assert 'status' not in updated
    assert app.call('memory.entry.get', {'id': entry['id']})['heads'] == updated['heads']
    with pytest.raises(ValueError):
        app.call('memory.entry.save', {'entry': {**entry, 'body': '过期覆盖'}, 'parents': entry['heads']})
    task = app.call('memory.entry.save', {'entry': {
        'kind': 'task', 'scope': 'project', 'project_id': project['id'], 'title': '任务', 'status': 'active'}, 'parents': []})
    app.call('memory.entry.save', {'entry': {**task, 'status': 'done'}, 'parents': task['heads']})
    assert len(app.call('memory.entries', {'project_id': project['id']})['entries']) == 1
    assert len(app.call('memory.entries', {'project_id': project['id'], 'include_done': True})['entries']) == 2
    assert app.call('memory.projects')['projects'][0]['available'] is True


def test_actual_snapshot_restore_reopens_same_store_without_foreign_locations(apps, tmp_path):
    original, target = apps
    project, entry, blob = create(original, tmp_path)
    original_identity = original.call('memory.info')
    target_identity = target.call('memory.info')
    assert original_identity['device_id'] != target_identity['device_id']
    package = tmp_path / 'memory.wtbak'
    finish(original, original.call('backups.export', {'path': str(package)}))
    with zipfile.ZipFile(package) as archive:
        assert 'project-memory/memory.sqlite3' in archive.namelist()
        assert 'project-memory/blobs/' + blob['hash'] in archive.namelist()
    old_store = target.project_memory
    finish(target, target.call('backups.import', {'path': str(package)}))
    assert target.project_memory is old_store
    assert target.call('memory.info')['library_id'] == original_identity['library_id']
    assert target.call('memory.info')['device_id'] == target_identity['device_id']
    assert target.call('memory.info')['replica_id'] != original_identity['replica_id']
    restored_project = target.call('memory.projects')['projects'][0]
    assert restored_project['locations'] == []
    assert restored_project['available'] is False
    restored = target.call('memory.entry.get', {'id': entry['id']})
    assert restored['body'] == '知识正文'
    assert target.project_memory.get_blob(blob['hash']).decode() == '已验证原始证据'
    updated = target.call('memory.entry.save', {'entry': {**restored, 'body': '恢复后继续编辑'}, 'parents': restored['heads']})
    assert target.call('memory.entry.get', {'id': updated['id']})['body'] == '恢复后继续编辑'
    # Other closures registered before replacement also use the reconnected object.
    promoted = target.call('memory.promote', {'id': updated['id']})
    assert promoted['promotion']['state'] == 'candidate'
    assert original.call('memory.entry.get', {'id': entry['id']})['body'] == '知识正文'


def test_markdown_export_real_job_contains_record_revision_and_attachments(apps, tmp_path):
    app, _ = apps
    project, entry, blob = create(app, tmp_path)
    row = finish(app, app.call('memory.export', {'project_id': project['id']}))
    assert row['result']['entry_count'] == 1
    artifact = row['artifacts'][0]
    assert Path(artifact['path']).is_file()
    with zipfile.ZipFile(artifact['path']) as archive:
        index = json.loads(archive.read('index.json'))
        assert index['library_id'] == app.project_memory.library_id
        assert index['entries'][0]['id'] == entry['id']
        markdown = archive.read(index['entries'][0]['file']).decode('utf-8')
        assert '知识正文' in markdown
        assert entry['heads'][0] in markdown
        assert archive.read('attachments/' + blob['hash']).decode('utf-8') == '已验证原始证据'
    assert app.call('memory.attachment.get', {'hash': blob['hash']})['size'] > 0


def test_principles_rpc_template_and_isolated_global_cas(apps, tmp_path, monkeypatch):
    from toolbox.project_memory.principles import START, END, DEFAULT_PROJECT_RULES
    app, _ = apps
    codex = tmp_path / 'isolated-codex-home'
    codex.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(codex))
    path = codex / 'AGENTS.md'
    prefix, suffix = '# 用户其他原则\n\n', '\n\n保留尾部原文。\n'
    path.write_text(prefix + START + '\n原原则\n' + END + suffix, encoding='utf-8')
    current = app.call('memory.principles.get', {'target': 'global'})
    assert current['text'] == '原原则'
    updated = app.call('memory.principles.save', {'target': 'global', 'text': '新原则', 'hash': current['hash']})
    assert path.read_text(encoding='utf-8') == prefix + START + '\n新原则\n' + END + suffix
    path.write_text(path.read_text(encoding='utf-8') + '外部编辑', encoding='utf-8')
    expected = path.read_bytes()
    with pytest.raises(ValueError, match='外部修改'):
        app.call('memory.principles.save', {'target': 'global', 'text': '过期覆盖', 'hash': updated['hash']})
    assert path.read_bytes() == expected
    template = app.call('memory.principles.get', {'target': 'template'})
    assert template['text'] == DEFAULT_PROJECT_RULES
    changed = app.call('memory.principles.save', {'target': 'template', 'text': '仅应用于新项目的模板', 'hash': template['hash']})
    assert changed['text'] == '仅应用于新项目的模板'
    with pytest.raises(ValueError, match='已修改'):
        app.call('memory.principles.save', {'target': 'template', 'text': '旧模板覆盖', 'hash': template['hash']})
    assert path.read_bytes() == expected


def test_board_covers_all_scopes_without_local_paths_and_migration_is_removed(apps):
    app, _ = apps
    assert not any(name.startswith('memory.migration.') for name in app.handlers)
    project = app.call('memory.project.create', {'name': '只在其他设备存在的项目'})
    assert not app.call('memory.projects')['projects'][0]['available']
    ids = set()
    for scope, status in [('project', 'active'), ('global', 'blocked'), ('global', 'done')]:
        entry = {'kind': 'task', 'scope': scope, 'title': scope, 'status': status}
        if scope == 'project':
            entry['project_id'] = project['id']
        ids.add(app.call('memory.entry.save', {'entry': entry, 'parents': []})['id'])
    rows = app.call('memory.entries', {'kind': 'task', 'include_done': True})['entries']
    assert {row['id'] for row in rows} == ids
    assert len(app.call('memory.entries', {'kind': 'task'})['entries']) == 2
