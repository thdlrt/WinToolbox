import json
import threading
import time
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.features.quick_menu import QuickMenu, translation_eligible


@pytest.fixture
def service(tmp_path):
    app = App(tmp_path / 'data', register_features=False, register_live=False)
    from toolbox.features.quick_menu import register
    register(app)
    value = QuickMenu(app)
    yield app, value
    app.close()


def finish(app, job):
    for _ in range(300):
        value = app.jobs.get(job['id'])
        if value['status'] not in ('queued', 'running', 'cancelling'):
            return value
        time.sleep(.01)
    raise AssertionError('job did not finish')


@pytest.mark.parametrize('text', ['performance', 'Hello world!', 'Bonjour tout le monde', 'こんにちは', 'Привет мир', 'This is a longer sentence about memory.'])
def test_plain_foreign_text_is_eligible(text):
    assert translation_eligible(text)


@pytest.mark.parametrize('text,target', [
    ('这是一段中文，API 调用不应该发生。','简体中文'),
    ('這是繁體中文內容。','zh-TW'), ('𠀀汉字测试', '中文'),
    ('これは日本語の文章です。','日语'), ('안녕하세요 반갑습니다','韩语'),
    ('This is a sentence about memory and performance.','English'), ('performance','en-US'),
])
def test_target_language_does_not_translate(text,target):
    assert not translation_eligible(text,target)


@pytest.mark.parametrize('text,target', [('这是一段中文。','English'), ('こんにちは世界','中文'),
    ('This is English.','中文'), ('Bonjour tout le monde','English'), ('你好 hello world','中文')])
def test_other_languages_and_substantial_mixed_text_translate(text,target):
    assert translation_eligible(text,target)


def test_target_language_guard_uses_current_settings_before_api(service,monkeypatch):
    app, service = service
    calls=[]
    monkeypatch.setattr(app.providers,'chat',lambda *a,**k:calls.append(a))
    context=service.capture({'text':'这是一段中文，API 不应调用。'})
    assert context['same_language'] and not context['foreign']
    with pytest.raises(ValueError,match='目标语言'): service.translate({'id':context['id']})
    context=service.capture({'text':'This is a sentence about memory.'})
    assert context['foreign']
    service.save({'target_language':'English'})
    assert not service.snapshot({'id':context['id']})['foreign']
    with pytest.raises(ValueError,match='目标语言'): service.translate({'id':context['id']})
    assert calls==[]


@pytest.mark.parametrize('text', ['', '纯中文', 'https://example.com/a?q=foo', 'ftp://example.xyz', 'example.xyz', 'hello@example.com',
                                 'Contact user@example.com today', 'password=abcd', 'api_key: sk-abcdefghijklmno',
                                 'aBcd1234!', 'abcdef123456', 'ghp_abcdefghijklmno', 'sk-12345678901234567890', 'C:\\notes\\file.txt'])
def test_addresses_and_credentials_never_auto_translate(text):
    assert not translation_eligible(text)


def test_protected_selection_is_discarded(service):
    app, _ = service
    context = app.call('quick.capture', {'text': 'secret words', 'protected': True})
    assert context['text'] == '' and not context['foreign']
    with pytest.raises(ValueError):
        app.call('quick.translate', {'id': context['id']})


def test_context_cap_and_trigger_rules(service, tmp_path):
    _, service = service
    folder = tmp_path / 'folder'; folder.mkdir()
    snapshot = service.capture({'paths': [str(folder)]})
    assert 'dissolve' in [a['id'] for a in snapshot['actions']]
    assert 'sha256' not in [a['id'] for a in snapshot['actions']]
    snapshot = service.capture({'text': 'hello world'})
    assert 'dissolve' not in [a['id'] for a in snapshot['actions']]
    assert 'join' in [a['id'] for a in snapshot['actions']]
    service.save({'actions': {'join': {'enabled': False, 'trigger': 'text'}}})
    assert 'join' not in [a['id'] for a in service.snapshot({'id': snapshot['id']})['actions']]
    for n in range(45): service.capture({'text': str(n)})
    assert len(service.contexts) == 32


def test_dissolve_is_previewed_and_preserves_files(service, tmp_path):
    app, _ = service
    root = tmp_path / 'selected'; root.mkdir()
    (root / 'a.txt').write_text('original')
    (root / 'nested').mkdir(); (root / 'nested' / 'b.txt').write_text('nested')
    context = app.call('quick.capture', {'paths': [str(root)]})
    params = {'id': context['id'], 'action': 'dissolve'}
    with pytest.raises(ValueError, match='预览'):
        app.call('quick.run', params)
    plan = app.call('quick.preview', params)
    assert root.exists() and not (tmp_path / 'a.txt').exists()
    result = finish(app, app.call('quick.run', {**params, 'token': plan['token']}))
    assert result['status'] == 'completed', result
    assert not root.exists()
    assert (tmp_path / 'a.txt').read_text() == 'original'
    assert (tmp_path / 'nested' / 'b.txt').read_text() == 'nested'
    assert result['artifacts']


def test_conflicts_stale_preview_and_nested_selection(service, tmp_path):
    app, _ = service
    root = tmp_path / 'selected'; root.mkdir()
    (root / 'a.txt').write_text('new'); (tmp_path / 'a.txt').write_text('keep')
    context = app.call('quick.capture', {'paths': [str(root)]})
    with pytest.raises(ValueError, match='同名'):
        app.call('quick.preview', {'id': context['id']})
    (tmp_path / 'a.txt').unlink()
    plan = app.call('quick.preview', {'id': context['id']})
    (root / 'extra.txt').write_text('changed')
    with pytest.raises(ValueError, match='预览'):
        app.call('quick.run', {'id': context['id'], 'action': 'dissolve', 'token': plan['token']})
    nested = root / 'nested'; nested.mkdir()
    both = app.call('quick.capture', {'paths': [str(root), str(nested)]})
    with pytest.raises(ValueError, match='父文件夹'):
        app.call('quick.preview', {'id': both['id']})


def test_dissolve_failure_rolls_back(service, tmp_path, monkeypatch):
    app, _ = service
    root = tmp_path / 'selected'; root.mkdir()
    (root / 'a.txt').write_text('A'); (root / 'b.txt').write_text('B')
    context = app.call('quick.capture', {'paths': [str(root)]})
    plan = app.call('quick.preview', {'id': context['id']})
    original = Path.rename
    def rename(path, target):
        if path == root / 'b.txt': raise PermissionError('locked')
        return original(path, target)
    monkeypatch.setattr(Path, 'rename', rename)
    result = finish(app, app.call('quick.run', {'id': context['id'], 'action': 'dissolve', 'token': plan['token']}))
    assert result['status'] == 'failed'
    assert (root / 'a.txt').read_text() == 'A' and (root / 'b.txt').read_text() == 'B'
    assert not (tmp_path / 'a.txt').exists()


def test_translation_deduplicates_and_does_not_persist_selection(service, monkeypatch):
    app, _ = service
    calls = []; release = threading.Event()
    def chat(messages, **kwargs):
        calls.append(messages); release.wait(3)
        return json.dumps({'translation': '性能', 'phonetic': '/pərˈfɔːrməns/', 'meanings': ['n. 表现；性能']})
    monkeypatch.setattr(app.providers, 'chat', chat)
    context = app.call('quick.capture', {'text': 'performance'})
    job = app.call('quick.translate', {'id': context['id']})
    assert app.call('quick.translate', {'id': context['id']})['id'] == job['id']
    release.set(); assert finish(app, job)['status'] == 'completed'
    assert app.call('quick.snapshot', {'id': context['id']})['translation']['translation'] == '性能'
    assert 'performance' not in json.dumps(app.jobs.get(job['id']))
    again = app.call('quick.capture', {'text': 'performance'})
    assert finish(app, app.call('quick.translate', {'id': again['id']}))['status'] == 'completed'
    assert len(calls) == 1


@pytest.mark.parametrize('action,text,expected', [('join','a\n b\t c','a b c'), ('json','{"a":1}','{\n  "a": 1\n}'),
                                                ('base64_encode','hello','aGVsbG8='), ('base64_decode','aGVsbG8=','hello')])
def test_text_scripts(service, action, text, expected):
    app, _ = service
    context = app.call('quick.capture', {'text': text})
    result = finish(app, app.call('quick.run', {'id': context['id'], 'action': action}))
    assert result['status'] == 'completed', result
    assert app.call('quick.snapshot', {'id': context['id']})['output'] == expected


def test_custom_script_uses_context_file_and_cleans_it(service, tmp_path):
    app, _ = service
    script = tmp_path / 'echo.py'
    script.write_text('import json,sys\nprint(json.load(open(sys.argv[2],encoding="utf-8"))["text"].upper())')
    app.call('quick.save', {'custom': [{'id':'custom-test','name':'Upper','trigger':'text','path':str(script),'enabled':True}]})
    context = app.call('quick.capture', {'text':'hi'})
    result = finish(app, app.call('quick.run', {'id':context['id'],'action':'custom-test'}))
    assert result['status'] == 'completed', result
    assert app.call('quick.snapshot', {'id':context['id']})['output'].strip() == 'HI'
    assert not list(app.data_dir.glob('jobs/*/selection.json'))


def test_aliyun_role_is_independent_and_preserves_existing_choices(service):
    app, _ = service
    app.settings.update({'roles': {'translate': {'provider_id': 'other', 'model': 'existing'}}})
    before = app.settings.get()
    QuickMenu(app)
    after = app.settings.get()
    assert after['roles']['translate'] == before['roles']['translate']
    assert after['roles']['quick_translate']['model'] == 'qwen3.8-flash'
    assert after['providers'] == before['providers']
