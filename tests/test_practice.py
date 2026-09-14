import base64
import json
import threading
import time
import wave
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from toolbox.app import App
from toolbox.features.practice import digest, split_sentences


def finish(app, job, expected='completed'):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = app.jobs.get(job['id'])
        if current['status'] not in ('queued', 'running', 'cancelling'):
            assert current['status'] == expected, current
            return current['result'] if expected == 'completed' else current
        time.sleep(.02)
    raise AssertionError('timed out')


@pytest.fixture
def app(tmp_path):
    app = App(tmp_path)
    app.tts_calls = []
    def tts(text, path, **kwargs):
        app.tts_calls.append((text, kwargs))
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\1\0' * 2400)
        return str(path)
    app.providers.tts = tts
    yield app
    app.close()
    app.jobs.pool.shutdown(wait=True)


def test_sentence_split_preserves_titles_decimals_and_long_content():
    assert split_sentences('Dr. Smith paid 3.50 dollars. Why?') == ['Dr. Smith paid 3.50 dollars.', 'Why?']
    text = 'English words ' * 90
    pieces = split_sentences(text)
    assert ' '.join(pieces) == text.strip()
    assert max(map(len, pieces)) <= 450
    with pytest.raises(ValueError):
        split_sentences('')


def test_repeat_and_sentence_selection_reuse_audio_and_reload(app):
    first = finish(app, app.call('practice.generate', {'text': 'Hello world. How are you?'}))
    again = finish(app, app.call('practice.generate', {'id': first['id']}))
    selected = finish(app, app.call('practice.fragment', {'id': first['id'], 'text': 'How are you?'}))
    assert len(app.tts_calls) == 2
    assert again['id'] == first['id'] and again['reused'] == 2 and selected['reused'] == 1
    audio = app.call('practice.audio', {'id': first['sentences'][0]['audio_id']})
    assert base64.b64decode(audio['data']).startswith(b'RIFF')
    reopened = App(app.data_dir)
    try:
        assert reopened.call('practice.get', {'id': first['id']})['text'] == first['text']
        assert len(reopened.call('practice.list')['items']) == 1
        assert reopened.call('practice.get', {'id': first['id']})['fragments'][0]['id'] == selected['id']
        assert selected['parent_id'] == first['id'] and selected['fragment_id'] == selected['id']
    finally:
        reopened.close()


def test_voice_and_model_changes_invalidate_cache(app):
    finish(app, app.call('practice.generate', {'text': 'Hello.', 'voice': 'Cherry'}))
    finish(app, app.call('practice.generate', {'text': 'Hello.', 'voice': 'Serena'}))
    app.settings.update({'roles': {'tts': {'model': 'another-model'}}})
    finish(app, app.call('practice.generate', {'text': 'Hello.', 'voice': 'Cherry'}))
    assert len(app.tts_calls) == 3


def test_duplicate_jobs_do_not_double_bill_and_paths_are_checked(app):
    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(lambda _: app.call('practice.generate', {'text': 'One sentence.'}), range(2)))
    for job in jobs:
        finish(app, job)
    assert len(app.tts_calls) == 1
    for method in ('practice.get', 'practice.audio'):
        with pytest.raises(ValueError):
            app.call(method, {'id': '../../settings'})


def test_local_mode_can_replay_but_cannot_send_to_api(app):
    item = finish(app, app.call('practice.generate', {'text': 'Hello.'}))
    app.settings.update({'preferences': {'model_mode': 'local'}})
    assert app.call('practice.audio', {'id': item['sentences'][0]['audio_id']})['data']
    job = app.call('practice.generate', {'text': 'Something new.'})
    for _ in range(100):
        current = app.jobs.get(job['id'])
        if current['status'] == 'failed':
            break
        time.sleep(.02)
    assert current['status'] == 'failed'
    assert len(app.tts_calls) == 1


def test_retry_keeps_already_paid_completed_sentences(app):
    original = app.providers.tts
    attempts = []
    def interrupted(text, path, **kwargs):
        attempts.append(text)
        if text == 'Second sentence.' and attempts.count(text) == 1:
            raise RuntimeError('Fixture connection interrupted')
        return original(text, path, **kwargs)
    app.providers.tts = interrupted
    job = app.call('practice.generate', {'text': 'First sentence. Second sentence.'})
    for _ in range(250):
        record = app.jobs.get(job['id'])
        if record['status'] == 'failed':
            break
        time.sleep(.02)
    assert record['status'] == 'failed'
    item = finish(app, app.jobs.retry(job['id']))
    assert item['reused'] == 1
    assert attempts.count('First sentence.') == 1
    assert attempts.count('Second sentence.') == 2


def test_save_edit_generate_keeps_identity_old_audio_and_unchanged_sentence(app):
    saved = app.call('practice.save', {'text': 'First sentence. Second sentence.'})
    assert len(saved['id']) == 64 and saved['status'] == 'draft' and not app.tts_calls
    first = finish(app, app.call('practice.generate', {'id': saved['id'], 'revision': saved['revision']}))
    fragment = finish(app, app.call('practice.fragment', {'id': first['id'], 'text': 'First sentence.'}))
    before = app.call('practice.get', {'id': first['id']})
    edited = app.call('practice.save', {'id': first['id'], 'revision': before['revision'], 'text': 'First sentence. A revised second sentence.'})
    assert edited['id'] == first['id'] and edited['audio_id'] == first['audio_id']
    assert edited['status'] == 'stale' and edited['stale'] and edited['fragments'][0]['stale']
    assert edited['content_revision'] == before['content_revision'] + 1
    assert Path(edited['path']).is_file()
    new = finish(app, app.call('practice.generate', {'id': edited['id']}))
    assert new['id'] == first['id'] and new['reused'] == 1 and not new['stale']
    assert len(app.tts_calls) == 3 and len(new['versions']) == 2
    assert new['versions'][0]['audio_id'] == first['audio_id']
    assert new['fragments'][0]['id'] == fragment['id']
    with pytest.raises(ValueError, match='已更新'):
        app.call('practice.save', {'id': first['id'], 'revision': before['revision'], 'text': 'Stale overwrite.'})


def test_folder_operations_do_not_dirty_audio_or_lose_fragments(app):
    folder = app.call('practice.folders.create', {'name': '../Names are not paths'})
    item = finish(app, app.call('practice.generate', {'text': 'Hello world.', 'folder_id': folder['id']}))
    fragment = finish(app, app.call('practice.fragment', {'id': item['id'], 'text': 'Hello'}))
    item = app.call('practice.get', {'id': item['id']})
    renamed = app.call('practice.save', {'id': item['id'], 'title': 'Greeting'})
    assert renamed['content_revision'] == item['content_revision'] and not renamed['stale']
    app.call('practice.folders.rename', {'id': folder['id'], 'name': 'Greetings'})
    assert app.call('practice.folders.list')['folders'][0]['item_count'] == 1
    result = app.call('practice.folders.delete', {'id': folder['id']})
    moved = app.call('practice.get', {'id': item['id']})
    assert result['moved_count'] == 1 and moved['folder_id'] is None
    assert moved['audio_id'] == item['audio_id'] and moved['content_revision'] == item['content_revision']
    assert not moved['stale'] and moved['fragments'][0]['id'] == fragment['id']
    assert len(app.call('practice.list', {'folder_id': None})['items']) == 1
    assert not app.call('practice.list', {'folder_id': folder['id']})['items']
    with pytest.raises(ValueError):
        app.call('practice.move', {'id': item['id'], 'folder_id': '../../other'})


def test_force_is_fresh_without_overwriting_shared_audio_then_stays_current(app):
    first = finish(app, app.call('practice.generate', {'text': 'A shared sentence.'}))
    other = finish(app, app.call('practice.generate', {'text': 'A shared sentence.'}))
    shared_bytes = Path(first['path']).read_bytes()
    assert first['id'] != other['id'] and first['audio_id'] == other['audio_id']
    forced = finish(app, app.call('practice.generate', {'id': first['id'], 'force': True}))
    assert forced['audio_id'] != first['audio_id']
    assert forced['sentences'][0]['audio_id'] != first['sentences'][0]['audio_id']
    assert Path(other['path']).read_bytes() == shared_bytes
    assert len(app.tts_calls) == 2
    again = finish(app, app.call('practice.generate', {'id': first['id']}))
    assert again['audio_id'] == forced['audio_id'] and again['reused'] == 1
    assert len(app.tts_calls) == 2


def test_deletion_archives_records_and_keeps_shared_audio(app):
    item = finish(app, app.call('practice.generate', {'text': 'Hello world.'}))
    fragment = finish(app, app.call('practice.fragment', {'id': item['id'], 'text': 'Hello'}))
    removed = app.call('practice.delete', {'id': item['id'], 'fragment_id': fragment['id']})
    assert Path(removed['archive_path']).is_file() and Path(fragment['path']).is_file()
    assert not app.call('practice.get', {'id': item['id']})['fragments']
    removed = app.call('practice.delete', {'id': item['id']})
    archived = json.loads(Path(removed['archive_path']).read_text('utf-8'))
    assert archived['record']['id'] == item['id'] and Path(item['path']).is_file()
    assert not app.call('practice.list')['items']
    with pytest.raises(ValueError, match='不存在'):
        app.call('practice.get', {'id': item['id']})


@pytest.mark.parametrize('action', ['edit', 'delete'])
def test_late_generation_never_overwrites_edit_or_resurrects_deleted_item(app, action):
    item = app.call('practice.save', {'text': 'The original sentence.'})
    entered, release = threading.Event(), threading.Event()
    original = app.providers.tts
    def blocked(text, path, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(text, path, **kwargs)
    app.providers.tts = blocked
    job = app.call('practice.generate', {'id': item['id']})
    assert entered.wait(2)
    if action == 'edit':
        changed = app.call('practice.save', {'id': item['id'], 'text': 'The edited sentence.'})
    else:
        app.call('practice.delete', {'id': item['id']})
    release.set()
    finish(app, job, expected='cancelled')
    finish(app, app.jobs.retry(job['id']), expected='cancelled')
    if action == 'edit':
        assert app.call('practice.get', {'id': item['id']})['text'] == changed['text']
    else:
        assert not app.call('practice.list')['items']
    # The paid raw response remains reusable by a new valid paragraph.
    app.providers.tts = original
    finish(app, app.call('practice.generate', {'text': 'The original sentence.'}))
    assert len(app.tts_calls) == 1


def test_move_during_generation_keeps_new_folder_and_finishes_without_extra_tts(app):
    item = app.call('practice.save', {'text': 'Keep generating.'})
    folder = app.call('practice.folders.create', {'name': 'Moved'})
    entered, release = threading.Event(), threading.Event()
    original = app.providers.tts
    def blocked(text, path, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(text, path, **kwargs)
    app.providers.tts = blocked
    job = app.call('practice.generate', {'id': item['id']})
    assert entered.wait(2)
    app.call('practice.move', {'id': item['id'], 'folder_id': folder['id']})
    release.set()
    result = finish(app, job)
    assert result['folder_id'] == folder['id'] and result['content_revision'] == item['content_revision']
    assert len(app.tts_calls) == 1


def test_fragment_deleted_while_generating_cannot_return_or_revive(app):
    item = app.call('practice.save', {'text': 'A selected word belongs here.'})
    entered, release = threading.Event(), threading.Event()
    original = app.providers.tts
    def blocked(text, path, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(text, path, **kwargs)
    app.providers.tts = blocked
    job = app.call('practice.fragment', {'id': item['id'], 'text': 'selected word'})
    assert entered.wait(2)
    pending = app.call('practice.get', {'id': item['id']})
    fragment_id = pending['fragments'][0]['id']
    with pytest.raises(ValueError, match='已更新'):
        app.call('practice.delete', {'id': item['id'], 'fragment_id': fragment_id, 'revision': item['revision']})
    app.call('practice.delete', {'id': item['id'], 'fragment_id': fragment_id, 'revision': pending['revision']})
    release.set()
    finish(app, job, expected='cancelled')
    finish(app, app.jobs.retry(job['id']), expected='cancelled')
    saved = app.call('practice.get', {'id': item['id']})
    assert saved['text'] == item['text'] and saved['fragments'] == []
    assert len(app.call('practice.list')['items']) == 1


def test_fragment_membership_handles_curly_apostrophes_and_rejects_other_text(app):
    item = app.call('practice.save', {'text': 'Please don’t leave. Practice well‑known words.'})
    fragment = finish(app, app.call('practice.fragment', {'id': item['id'], 'text': "don't"}))
    assert fragment['parent_id'] == item['id'] and fragment['text'] == "don't"
    finish(app, app.call('practice.fragment', {'id': item['id'], 'text': 'well-known'}))
    with pytest.raises(ValueError, match='不在当前段落'):
        app.call('practice.fragment', {'id': item['id'], 'text': 'unrelated material'})
    assert len(app.call('practice.list')['items']) == 1


def test_legacy_hash_item_remains_standalone_and_playable(app):
    item = finish(app, app.call('practice.generate', {'text': 'Legacy sentence.'}))
    legacy_id = digest({'old': 'hash-identified practice'})
    root = app.data_dir / 'practice'
    (root / 'audio' / (legacy_id + '.wav')).write_bytes(Path(item['path']).read_bytes())
    legacy = {k: item[k] for k in ('text', 'voice', 'model', 'created_at', 'sentences')}
    legacy['id'] = legacy_id
    (root / 'items' / (legacy_id + '.json')).write_text(json.dumps(legacy), encoding='utf-8')
    restored = app.call('practice.get', {'id': legacy_id})
    assert restored['id'] == legacy_id and restored['audio_id'] == legacy_id and restored['folder_id'] is None
    assert restored['fragments'] == [] and app.call('practice.audio', {'id': legacy_id})['data']
    result = finish(app, app.call('practice.generate', {'id': legacy_id, 'text': 'Legacy sentence. Another sentence.'}))
    assert result['id'] == legacy_id and result['reused'] == 1
    assert result['versions'][0]['audio_id'] == legacy_id
    assert app.call('practice.audio', {'id': legacy_id}) == app.call('practice.audio', {'id': result['audio_id']})
    assert len(app.call('practice.list')['items']) == 2
