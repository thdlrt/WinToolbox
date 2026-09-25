from pathlib import Path

import pytest

from toolbox.project_memory import MemoryStore
from toolbox.project_memory.principles import DEFAULT_PROJECT_RULES, END, START, initialize_project_rules, read_rules, save_rules


@pytest.fixture
def store(tmp_path):
    instance = MemoryStore(tmp_path / 'memory', device_id='principles-fixture')
    try:
        yield instance
    finally:
        instance.close()


def test_project_first_init_preserves_existing_instructions_and_never_overwrites(store, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    path = project / 'AGENTS.md'
    outside = '# Existing project instructions\r\n\r\nKeep custom tests.\r\n'
    path.write_bytes(outside.encode())
    first = initialize_project_rules(store, project)
    assert first['initialized'] is True
    assert first['text'].replace('\r\n', '\n') == DEFAULT_PROJECT_RULES
    assert path.read_bytes().startswith(outside.encode())
    changed = save_rules(path, '## 用户自定原则\n\n保留我的修改', first['hash'])
    before = path.read_bytes()
    second = initialize_project_rules(store, project)
    assert second['initialized'] is False
    assert path.read_bytes() == before
    assert second['text'] == changed['text']


def test_template_applies_to_new_projects_only(store, tmp_path):
    store.save_entry({'id': 'memory-project-template', 'title': '项目原则模板', 'body': '## 项目原则\n\n- 自定义默认原则。',
                      'scope': 'global', 'kind': 'knowledge', 'internal': True, 'archived': True})
    project = tmp_path / 'new-project'
    project.mkdir()
    result = initialize_project_rules(store, project)
    assert result['text'] == '## 项目原则\n\n- 自定义默认原则。'
    assert read_rules(project / 'AGENTS.md')['exists'] is True
    assert not (tmp_path / 'AGENTS.md').exists()


def test_save_preserves_both_outside_sections_and_rejects_external_edit(tmp_path):
    path = tmp_path / 'AGENTS.md'
    prefix, suffix = '# Custom\nKeep before.\n', '\nKeep after.\n'
    path.write_text(prefix + START + '\nold\n' + END + suffix, encoding='utf-8')
    initial = read_rules(path)
    assert initial['text'] == 'old'
    current = save_rules(path, '新原则', initial['hash'])
    assert path.read_text(encoding='utf-8') == prefix + START + '\n新原则\n' + END + suffix
    path.write_text(path.read_text(encoding='utf-8') + '\nexternal edit\n', encoding='utf-8')
    expected = path.read_bytes()
    with pytest.raises(ValueError, match='外部修改'):
        save_rules(path, '覆盖', current['hash'])
    assert path.read_bytes() == expected
    assert not list(tmp_path.glob('*.lock'))


def test_first_creation_and_invalid_markers_and_missing_directory(store, tmp_path):
    path = tmp_path / 'AGENTS.md'
    empty = read_rules(path)
    assert empty['exists'] is False and empty['text'] == ''
    save_rules(path, 'first', empty['hash'])
    for text in (START + '\nmissing end', END + START, START + END + START + END):
        path.write_text(text, encoding='utf-8')
        with pytest.raises(ValueError, match='标记'):
            read_rules(path)
    path.write_text('outside only', encoding='utf-8')
    before = read_rules(path)
    with pytest.raises(ValueError, match='管理标记'):
        save_rules(path, START + END, before['hash'])
    with pytest.raises(ValueError, match='目录不存在'):
        initialize_project_rules(store, tmp_path / 'missing')
    assert not (tmp_path / 'missing').exists()
