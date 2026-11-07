import json
import threading
import time

import pytest

from toolbox.app import App
from toolbox.features import clipboard_ocr
from toolbox.features.quick_menu import QuickMenu


class Clipboard:
    item = ('text', 'Hello world')
    reads = 0

    def read_text(self):
        self.reads += 1
        return 1, self.item


@pytest.fixture
def service(tmp_path, monkeypatch):
    events = []
    app = App(tmp_path / 'data', emit=events.append, register_features=False, register_live=False)
    menu = QuickMenu(app)
    app.jobs.register('quick.translate', menu.translate_job)
    app.jobs.register('quick.action', menu.run_job)
    adapter = Clipboard()
    monkeypatch.setattr(clipboard_ocr, 'clipboard', lambda app: adapter)
    calls = []
    def chat(messages, **kwargs):
        calls.append((messages, kwargs['role']))
        return json.dumps({'translation': '你好，世界', 'phonetic': '', 'meanings': []}, ensure_ascii=False)
    monkeypatch.setattr(app.providers, 'chat', chat)
    yield app, menu, adapter, calls, events
    app.close()


def finish(app, job):
    for _ in range(500):
        result = app.jobs.get(job['id'])
        if result['status'] not in ('queued', 'running', 'cancelling'):
            return result
        time.sleep(.01)
    raise AssertionError('translation did not finish')


def test_capture_reads_clipboard_only_when_requested_and_preserves_selection(service):
    app, menu, adapter, calls, _ = service
    menu.capture({'text': '选区原文'})
    assert adapter.reads == 0
    context = menu.capture({'text': '选区原文', 'clipboard': True})
    assert context['text'] == '选区原文' and context['clipboard_text'] == 'Hello world'
    assert context['clipboard_foreign'] and not calls
    result = finish(app, menu.translate({'id': context['id'], 'source': 'clipboard'}))
    assert result['status'] == 'completed', result
    after = menu.snapshot({'id': context['id']})
    assert after['clipboard_translation']['translation'] == '你好，世界'
    assert after['translation'] is None and after['text'] == '选区原文'
    assert calls[0][1] == 'quick_translate'
    assert adapter.item == ('text', 'Hello world')


@pytest.mark.parametrize('text', ['纯中文内容', '繁體中文內容', '中文 API 测试', '12345', ''])
def test_chinese_sensitive_and_empty_clipboard_do_not_auto_translate(service, text):
    _, menu, adapter, calls, _ = service
    adapter.item = ('text', text)
    context = menu.capture({'clipboard': True})
    assert not context['clipboard_foreign'] and not calls


@pytest.mark.parametrize('text', ['https://example.com', 'password: Abcd12345!'])
def test_links_and_credentials_in_clipboard_translate_too(service, text):
    app, menu, adapter, calls, _ = service
    adapter.item = ('text', text)
    context = menu.capture({'clipboard': True})
    assert context['clipboard_foreign']
    result = finish(app, menu.translate({'id': context['id'], 'source': 'clipboard'}))
    assert result['status'] == 'completed'
    assert calls and calls[0][0][1]['content'] == text


def test_manual_chinese_skips_api_and_clipboard_target_stays_chinese(service):
    app, menu, adapter, calls, _ = service
    menu.save({'target_language': 'English'})
    adapter.item = ('text', '这是中文内容')
    context = menu.capture({})
    result = finish(app, menu.run({'id': context['id'], 'action': 'clipboard_translate'}))
    assert result['status'] == 'completed' and result['result']['skipped'] == 'target_language'
    assert not calls
    adapter.item = ('text', 'Hello world')
    result = finish(app, menu.run({'id': context['id'], 'action': 'clipboard_translate'}))
    assert result['status'] == 'completed', result
    assert '简体中文' in calls[0][0][0]['content']


def test_explicit_action_reads_latest_and_does_not_persist_clipboard_text(service):
    app, menu, adapter, calls, events = service
    context = menu.capture({'clipboard': True})
    adapter.item = ('text', 'Please open the window.')
    result = finish(app, menu.run({'id': context['id'], 'action': 'clipboard_translate'}))
    assert result['status'] == 'completed', result
    assert calls[0][0][1]['content'] == 'Please open the window.'
    assert 'Please open' not in json.dumps(app.jobs.list())
    assert 'Please open' not in json.dumps(events)
    assert '你好' not in json.dumps(app.jobs.list(), ensure_ascii=False)


def test_cache_shared_with_selected_text_and_disabled_clipboard_action(service):
    app, menu, _, calls, _ = service
    context = menu.capture({'text': 'Hello world', 'clipboard': True})
    assert finish(app, menu.translate({'id': context['id']}))['status'] == 'completed'
    assert finish(app, menu.translate({'id': context['id'], 'source': 'clipboard'}))['status'] == 'completed'
    assert len(calls) == 1
    menu.save({'actions': {'clipboard_translate': {'enabled': False, 'trigger': 'always'}}})
    assert not menu.snapshot({'id': context['id']})['clipboard_foreign']
    with pytest.raises(ValueError, match='禁用'):
        menu.translate({'id': context['id'], 'source': 'clipboard'})


def test_protected_context_does_not_read_clipboard_and_busy_does_not_block_menu(service, monkeypatch):
    _, menu, adapter, _, _ = service
    context = menu.capture({'clipboard': True, 'protected': True})
    assert adapter.reads == 0 and not context['clipboard_text'] and not context['clipboard_foreign']
    def busy():
        raise OSError('clipboard busy')
    monkeypatch.setattr(adapter, 'read_text', busy)
    context = menu.capture({'clipboard': True})
    assert not context['clipboard_text']
    with pytest.raises(OSError, match='busy'):
        menu.run({'id': context['id'], 'action': 'clipboard_translate'})


@pytest.mark.parametrize('item', [None, ('image', b'image'), ('text', 'a' * 6001)])
def test_empty_image_or_overlong_clipboard_is_not_sent_to_api(service, item):
    _, menu, adapter, calls, _ = service
    adapter.item = item
    context = menu.capture({'clipboard': True})
    assert not context['clipboard_foreign']
    with pytest.raises(ValueError):
        menu.run({'id': context['id'], 'action': 'clipboard_translate'})
    assert not calls


def test_deduplication_and_changed_text_does_not_publish_old_result(service, monkeypatch):
    app, menu, adapter, _, _ = service
    entered, release = threading.Event(), threading.Event()
    def chat(messages, **kwargs):
        entered.set()
        release.wait(3)
        return json.dumps({'translation': '旧译文'})
    monkeypatch.setattr(app.providers, 'chat', chat)
    context = menu.capture({'clipboard': True})
    job = menu.translate({'id': context['id'], 'source': 'clipboard'})
    assert entered.wait(2)
    assert menu.translate({'id': context['id'], 'source': 'clipboard'})['id'] == job['id']
    adapter.item = ('text', 'This is a new sentence.')
    menu._read_clipboard_text(menu.context({'id': context['id']}))
    release.set()
    assert finish(app, job)['result']['skipped'] == 'superseded'
    assert menu.snapshot({'id': context['id']})['clipboard_translation'] is None


def test_ocr_result_becomes_eligible_for_following_translation(service, monkeypatch):
    app, menu, adapter, calls, _ = service
    adapter.item = ('image', b'image')
    adapter.sequence = lambda: 1
    adapter.read = lambda: (1, adapter.item)
    def write(kind, text, **kwargs):
        adapter.item = (kind, text)
    adapter.write = write
    monkeypatch.setattr(clipboard_ocr, 'recognize', lambda *args: 'Hello world')
    context = menu.capture({})
    assert finish(app, menu.run({'id': context['id'], 'action': 'clipboard_ocr'}))['status'] == 'completed'
    context = menu.snapshot({'id': context['id']})
    assert context['clipboard_foreign'] and context['output'] == context['clipboard_text'] == 'Hello world'
    assert finish(app, menu.translate({'id': context['id'], 'source': 'clipboard'}))['status'] == 'completed'
    assert adapter.item == ('text', 'Hello world') and len(calls) == 1


def test_api_failure_is_reported_without_fake_translation(service, monkeypatch):
    app, menu, adapter, _, _ = service
    def unavailable(*args, **kwargs):
        raise RuntimeError('API HTTP 401: invalid key')
    monkeypatch.setattr(app.providers, 'chat', unavailable)
    context = menu.capture({'clipboard': True})
    job = finish(app, menu.translate({'id': context['id'], 'source': 'clipboard'}))
    assert job['status'] == 'failed' and '401' in job['error']
    assert menu.snapshot({'id': context['id']})['clipboard_translation'] is None
    assert adapter.item == ('text', 'Hello world')


def test_deferred_clipboard_read_does_not_hold_capture_or_queue_readers(service, monkeypatch):
    _, menu, adapter, _, _ = service
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        assert release.wait(3)
        return 1, ('text', 'Hello world')
    monkeypatch.setattr(adapter, 'read_text', slow)
    first = menu.capture({})
    results = []
    reader = threading.Thread(target=lambda: results.append(menu.clipboard_snapshot({'id': first['id']})))
    reader.start()
    try:
        assert entered.wait(2)
        # Opening another menu and refreshing it complete before the blocked OS read.
        second = menu.capture({'text': 'new selection', 'clipboard': False})
        skipped = menu.clipboard_snapshot({'id': second['id']})
        assert skipped['text'] == 'new selection' and not skipped['clipboard_text']
    finally:
        release.set()
        reader.join(3)
    assert not reader.is_alive()
    assert results[0]['clipboard_text'] == 'Hello world'


def test_deferred_refresh_respects_protected_context(service):
    _, menu, adapter, _, _ = service
    context = menu.capture({'protected': True})
    assert not menu.clipboard_snapshot({'id': context['id']})['clipboard_text']
    assert adapter.reads == 0


def test_late_clipboard_read_does_not_replace_newer_ocr_or_manual_result(service, monkeypatch):
    _, menu, adapter, _, _ = service
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        assert release.wait(3)
        return 1, ('text', 'stale clipboard')
    monkeypatch.setattr(adapter, 'read_text', slow)
    snapshot = menu.capture({})
    context = menu.context({'id': snapshot['id']})
    reader = threading.Thread(target=lambda: menu.clipboard_snapshot({'id': snapshot['id']}))
    reader.start()
    try:
        assert entered.wait(2)
        menu._set_clipboard_text(context, 'new OCR result')
    finally:
        release.set()
        reader.join(3)
    assert menu.snapshot({'id': snapshot['id']})['clipboard_text'] == 'new OCR result'


def test_deferred_clipboard_public_entry_preserves_selection_and_does_not_translate(service):
    app, _, adapter, calls, _ = service
    from toolbox.features.quick_menu import register
    register(app)
    context = app.call('quick.capture', {'text': '选区原文', 'clipboard': False})
    assert adapter.reads == 0
    refreshed = app.call('quick.clipboard', {'id': context['id']})
    assert refreshed['text'] == '选区原文' and refreshed['clipboard_text'] == 'Hello world'
    assert refreshed['clipboard_foreign'] and not calls
