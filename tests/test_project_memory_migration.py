import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolbox.project_memory.store import MemoryStore
from toolbox.project_memory_migration import Migration
from toolbox.project_memory_curation import Curation


def legacy(path, id, kind='knowledge', title='知识', project='p', status='active', extra='', body='正文'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nak_version: 1\nak_id: {id}\nkind: {kind}\nscope: project\ntitle: {title}\nproject_id: {project}\nstatus: {status}\nrelated_ids: []\n{extra}---\n{body}', encoding='utf-8')


def setup(tmp_path):
    central = tmp_path / 'central'
    project = tmp_path / 'project'
    legacy(central / 'Projects/p.md', 'p', 'project', extra=f'project_root: {project.as_posix()}\ncontext_dir: .agent\n')
    legacy(project / '.agent/manifest.md', 'p', 'project')
    legacy(project / '.agent/archive/t.md', 't', 'task', status='archived')
    legacy(project / '.agent/knowledge/k.md', 'k', extra='knowledge_type: procedure\ncustom: 保留\n')
    store = MemoryStore(tmp_path / 'db', device_id='test')
    return central, project, store, Migration(store, tmp_path / 'migrations')


def test_migration_identity_idempotency_and_preserved_sources(tmp_path):
    central, project, store, migration = setup(tmp_path)
    source = (project / '.agent/knowledge/k.md').read_bytes()
    report = migration.preview(str(central))
    assert report['record_count'] == 3
    result = migration.apply(report['preview_id'])
    assert result['bound_count'] == 1
    assert store.get_entry('t')['status'] == 'done'
    assert store.get_entry('k')['legacy']['custom'] == '保留'
    assert store.get_entry('k')['knowledge_type'] == 'procedure'
    assert (project / '.agent/knowledge/k.md').read_bytes() == source
    count = store.info()['operation_count']
    assert migration.apply(report['preview_id']) == result
    assert store.info()['operation_count'] == count
    another = migration.preview(str(central))
    migration.apply(another['preview_id'])
    assert store.info()['operation_count'] == count


def test_migration_source_drift_and_missing_project(tmp_path):
    central, project, store, migration = setup(tmp_path)
    report = migration.preview(str(central))
    p = project / '.agent/knowledge/k.md'
    p.write_text(p.read_text() + 'changed')
    with pytest.raises(ValueError, match='变化'):
        migration.apply(report['preview_id'])
    assert store.info()['operation_count'] == 0


def test_migration_same_id_keeps_conflict(tmp_path):
    central, project, store, migration = setup(tmp_path)
    legacy(project / '.agent/knowledge/duplicate.md', 'k', body='不同正文')
    report = migration.preview(str(central))
    migration.apply(report['preview_id'])
    assert store.get_entry('k')['conflict']
    assert len(store.get_entry('k')['heads']) == 2


class Job:
    def __init__(self, id): self.params = {'id': id}
    def progress(self, *args): pass
    def check_cancelled(self): pass
    def mark_committed(self): pass


def test_curation_requires_valid_response_and_source_revision(tmp_path):
    store = MemoryStore(tmp_path, device_id='test')
    store.create_project('项目', 'p')
    source = store.save_entry({'id':'k','kind':'knowledge','scope':'project','project_id':'p','title':'经验','body':'证据'})
    app = SimpleNamespace(providers=SimpleNamespace(chat=lambda *a, **kw: '{"action":"accept","title":"可复用经验","body":"验证范围：本项目","reason":"有证据"}'))
    curator = Curation(app, store)
    candidate = curator.promote('k')
    assert curator.promote('k')['id'] == candidate['id']
    result = curator.curate(Job(candidate['id']))
    assert result['entry']['promotion']['state'] == 'accepted'
    assert store.get_entry('k')['scope'] == 'project'
    candidate2_source = store.save_entry({**source,'body':'修改'}, parents=source['heads'])
    candidate2 = curator.promote('k')
    store.save_entry({**candidate2_source,'body':'再次修改'}, parents=candidate2_source['heads'])
    with pytest.raises(ValueError, match='源知识'):
        curator.curate(Job(candidate2['id']))
