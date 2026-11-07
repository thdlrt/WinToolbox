import io
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PIL import Image

from toolbox.app import App
from toolbox.features import clipboard_ocr, quick_menu
from toolbox.features.clipboard_history import WindowsClipboard


class Clipboard:
    def __init__(self):
        self.seq = 1
        self.item = ('image', b'image')
        self.writes = []

    def sequence(self):
        return self.seq

    def read(self):
        return self.seq, self.item

    def read_text(self):
        return self.seq, self.item if self.item and self.item[0] == 'text' else None

    def write(self, kind, value, *, expected_sequence):
        if self.seq != expected_sequence:
            raise ValueError('剪贴板已变化')
        self.writes.append((kind, value))
        self.seq += 1


@pytest.fixture
def service(tmp_path):
    app = App(tmp_path / 'data', register_features=False, register_live=False)
    quick_menu.register(app)
    adapter = Clipboard()
    app.clipboard_history = SimpleNamespace(adapter=adapter)
    yield app, adapter
    del app.clipboard_history
    app.close()


def finish(app, job):
    for _ in range(500):
        value = app.jobs.get(job['id'])
        if value['status'] not in ('queued', 'running', 'cancelling'):
            return value
        time.sleep(.01)
    raise AssertionError('OCR did not finish')


def start(app):
    context = app.call('quick.capture', {})
    assert 'clipboard_ocr' in [a['id'] for a in context['actions']]
    job = app.call('quick.run', {'id': context['id'], 'action': 'clipboard_ocr'})
    return context, job


def test_ocr_copies_full_text_without_persisting_payload(service, monkeypatch):
    app, adapter = service
    text = '识别结果\nHello 123\n' + '长文本' * 6000
    monkeypatch.setattr(clipboard_ocr, 'recognize', lambda job, data: text)
    context, job = start(app)
    result = finish(app, job)
    assert result['status'] == 'completed', result
    assert adapter.writes == [('text', text)]
    snapshot = app.call('quick.snapshot', {'id': context['id']})
    assert snapshot['output'] == text
    assert not snapshot.get('output_notice')
    assert '识别结果' not in json.dumps(result, ensure_ascii=False)
    assert list((app.data_dir / 'jobs' / job['id']).iterdir()) == []


@pytest.mark.parametrize('item', [None, ('text', 'original')])
def test_no_image_does_not_change_clipboard(service, monkeypatch, item):
    app, adapter = service
    adapter.item = item
    monkeypatch.setattr(clipboard_ocr, 'recognize', lambda *args: pytest.fail('must not OCR text'))
    _, job = start(app)
    result = finish(app, job)
    assert result['status'] == 'failed' and '没有图片' in result['error']
    assert not adapter.writes


@pytest.mark.parametrize('failure', ['empty', 'exception'])
def test_recognition_failure_preserves_image(service, monkeypatch, failure):
    app, adapter = service
    def recognize(*args):
        if failure == 'exception':
            raise RuntimeError('OCR unavailable')
        return ''
    monkeypatch.setattr(clipboard_ocr, 'recognize', recognize)
    _, job = start(app)
    assert finish(app, job)['status'] == 'failed'
    assert adapter.item[0] == 'image' and not adapter.writes


def test_clipboard_changed_during_ocr_keeps_result_for_manual_copy(service, monkeypatch):
    app, adapter = service
    def recognize(*args):
        adapter.seq += 1
        adapter.item = ('text', 'new user content')
        return '识别结果'
    monkeypatch.setattr(clipboard_ocr, 'recognize', recognize)
    context, job = start(app)
    assert finish(app, job)['status'] == 'failed'
    assert adapter.item == ('text', 'new user content') and not adapter.writes
    snapshot = app.call('quick.snapshot', {'id': context['id']})
    assert snapshot['output'] == '识别结果'
    assert snapshot['output_notice'] == '已识别，尚未写入剪贴板'


def test_duplicate_clicks_share_job_and_cancel_preserves_clipboard(service, monkeypatch):
    app, adapter = service
    entered, release = threading.Event(), threading.Event()
    def recognize(*args):
        entered.set()
        release.wait(3)
        return '识别结果'
    monkeypatch.setattr(clipboard_ocr, 'recognize', recognize)
    context, job = start(app)
    assert entered.wait(2)
    assert app.call('quick.run', {'id': context['id'], 'action': 'clipboard_ocr'})['id'] == job['id']
    app.jobs.cancel(job['id'])
    release.set()
    assert finish(app, job)['status'] == 'cancelled'
    assert not adapter.writes


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('format', ['PNG', 'DIB'])
def test_recognize_normalizes_and_always_cleans_temp_files(tmp_path, fail, format):
    data = io.BytesIO()
    Image.new('RGB', (10, 10), 'white').save(data, format)
    def run_process(args, **kwargs):
        source = args[args.index('-InputPath') + 1]
        assert Image.open(source).format == 'PNG'
        if fail:
            raise RuntimeError('OCR_LANGUAGE_MISSING')
        result = args[args.index('-ResultPath') + 1]
        result.write_text(json.dumps({'text': '你 好 世界\nHello world'}), encoding='utf-8')
    job = SimpleNamespace(work_dir=tmp_path, check_cancelled=lambda: None, run_process=run_process)
    if fail:
        with pytest.raises(RuntimeError, match='语言选项'):
            clipboard_ocr.recognize(job, data.getvalue())
    else:
        assert clipboard_ocr.recognize(job, data.getvalue()) == '你好世界\nHello world'
    assert list(tmp_path.iterdir()) == []


def test_native_write_checks_sequence_while_clipboard_is_open(monkeypatch):
    adapter = WindowsClipboard.__new__(WindowsClipboard)
    opened = []
    @contextmanager
    def locked():
        opened.append(True)
        try:
            yield
        finally:
            opened.pop()
    def sequence():
        assert opened
        return 2
    freed = []
    adapter.opened, adapter.sequence = locked, sequence
    adapter.k = SimpleNamespace(GlobalAlloc=lambda *a: 1, GlobalLock=lambda *a: 1,
                                GlobalUnlock=lambda *a: None, GlobalFree=lambda h: freed.append(h))
    adapter.u = SimpleNamespace(EmptyClipboard=lambda: pytest.fail('must not empty changed clipboard'))
    monkeypatch.setattr('toolbox.features.clipboard_history.c.memmove', lambda *a: None)
    with pytest.raises(ValueError, match='剪贴板已变化'):
        adapter.write('text', 'result', expected_sequence=1)
    assert freed == [1] and not opened
