import io
from pathlib import Path
import pytest
from PIL import Image
from toolbox.features.clipboard_history import ClipboardHistory

@pytest.fixture
def history(tmp_path):
    s = ClipboardHistory(tmp_path)
    yield s
    s.close()

def test_text_search_dedup_and_restart(history):
    ident = history.capture('text', '你好 Straße\nsecond line')
    history.capture('text', '你好 Straße\nsecond line')
    assert len(history.call('list', {'query': 'STRASSE'})['items']) == 1
    assert history.call('get', {'id': ident})['text'].endswith('second line')
    other = ClipboardHistory(history.root.parent)
    try: assert len(other.call('list', {})['items']) == 1
    finally: other.close()

def test_images_filters_delete(history):
    data = io.BytesIO(); Image.new('RGB', (20, 10), 'red').save(data, 'PNG')
    ident = history.capture('image', data.getvalue())
    history.capture('text', 'a')
    row = history.call('list', {'kind': 'image'})['items'][0]
    assert (row['width'], row['height']) == (20, 10)
    assert row['thumbnail']
    assert history.call('list', {'kind': 'image', 'query': 'a'})['items'] == []
    history.call('delete', {'id':ident})
    with pytest.raises(ValueError): history.call('get', {'id': ident})

def test_retention_reduction_and_config(history):
    now = [1000000.0]; history.clock = lambda: now[0]
    history.capture('text', 'old')
    now[0] += 86400 * 2
    assert len(history.call('list', {})['items']) == 1
    history.call('settings', {'days':1})
    assert history.call('list', {})['items'] == []
    for days in (0, 366, True, 1.5):
        with pytest.raises(ValueError): history.call('settings', {'days':days})

def test_pagination_and_clear(history):
    for i in range(55): history.capture('text', str(i))
    assert history.call('list', {})['more']
    assert len(history.call('list', {'offset':50})['items']) == 5
    with pytest.raises(ValueError): history.call('clear', {})
    history.call('clear', {'confirm':True})
    assert history.call('list', {})['items'] == []

def test_monitor_pause_retry_and_self_copy(history):
    class Adapter:
        seq = 1
        fail = False
        def sequence(self): return self.seq
        def read(self):
            if self.fail: raise OSError('busy')
            return self.seq, ('text', 'captured')
        def write(self, kind, payload): self.seq += 1; return self.seq
    history.adapter = a = Adapter()
    history.tick()
    ident = history.call('list', {})['items'][0]['id']
    history.call('copy', {'id':ident}); history.tick()
    assert len(history.call('list', {})['items']) == 1
    history.call('settings', {'enabled':False}); a.seq += 1; history.tick()
    history.call('settings', {'enabled':True}); assert history.seq == a.seq
    a.seq += 1; a.fail = True
    with pytest.raises(OSError): history.tick()
    assert history.seq != a.seq
    a.fail = False; history.tick(); assert history.seq == a.seq

def test_invalid_config_is_atomic_and_expired_image_removed(history):
    with pytest.raises(ValueError): history.call('settings', {'days':1, 'enabled':'yes'})
    assert history.call('settings', {})['days'] == 3
    now = [1000000.0]; history.clock = lambda: now[0]
    data = io.BytesIO(); Image.new('RGBA', (5, 5), (1,2,3,4)).save(data, 'PNG')
    history.capture('image', data.getvalue())
    now[0] += 3 * 86400
    history.cleanup()
    assert history.db.execute('SELECT COUNT(*) FROM items').fetchone()[0] == 0
