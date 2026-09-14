"""Ephemeral desktop captions: isolation, bounded work and model-mode routing."""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from toolbox.live import CaptionSession, CloudASR, LiveSession, LocalASR, register
from toolbox.settings import Settings


def eventually(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.005)
    assert condition()


@pytest.fixture
def app(tmp_path, monkeypatch):
    handlers, events, captures, calls = {}, [], [], []
    settings = Settings(tmp_path)
    monkeypatch.setattr(settings, "secret", lambda _: "fixture-key")
    def chat(messages, **kwargs):
        calls.append((messages, kwargs))
        if kwargs.get("cancel"):
            kwargs["cancel"]()
        return "这是译文"
    instance = SimpleNamespace(data_dir=tmp_path, settings=settings,
        jobs=SimpleNamespace(register=lambda *a: None, gpu_lock=threading.Lock()),
        models=SimpleNamespace(require=lambda *a, **kw: {}), providers=SimpleNamespace(chat=chat),
        emit=lambda type, **value: events.append({"type": type, **value}),
        register=lambda method, handler: handlers.update({method: handler}),
        call=lambda method, params=None: handlers[method](params or {}),
        events=events, captures=captures, calls=calls)
    monkeypatch.setattr(CloudASR, "run", lambda self: None)
    monkeypatch.setattr(LocalASR, "run", lambda self: None)
    monkeypatch.setattr(LiveSession, "capture", lambda self, source, asr: captures.append((source, self.params["record_audio"], type(asr))))
    register(instance)
    yield instance
    session = instance.live_holder["session"]
    if session:
        session.stop()


def test_captions_force_system_no_recording_no_qa_or_history(app):
    state = app.call("captions.start", {"source": "both", "record_audio": True, "auto_answer": True})
    session = app.live_holder["session"]
    assert state["active"] and state["session_id"] == session.id
    assert app.captures == [("system", False, CloudASR)]
    session.segment("one", "Why is the sky blue?", True, "system", 0, 2)
    eventually(lambda: session.segments["one"]["translation"])
    assert len(app.calls) == 1 and app.calls[0][1]["role"] == "translate"
    assert "is_question" not in app.calls[0][0][0]["content"]
    assert not session.answers
    assert not any(event["type"].startswith("live.") for event in app.events)
    assert app.call("live.history")["sessions"] == []
    assert not (app.data_dir / "sessions").exists()
    with pytest.raises(ValueError, match="启动实时会话"):
        app.call("live.answer", {"question": "Do not answer"})


def test_partial_dedup_final_translation_and_order(app):
    entered, release = threading.Event(), threading.Event()
    def chat(messages, **kwargs):
        if messages[-1]["content"] == "first":
            entered.set()
            assert release.wait(2)
        return messages[-1]["content"] + " translated"
    app.providers.chat = chat
    app.call("captions.start")
    s = app.live_holder["session"]
    s.segment("1", "fir", False, "system", 0, 1)
    revision = app.call("captions.state")["revision"]
    s.segment("1", "fir", False, "system", 0, 1)
    assert app.call("captions.state")["revision"] == revision
    s.segment("1", "first", True, "system", 0, 2)
    assert entered.wait(2)
    s.segment("2", "second", True, "system", 2, 4)
    eventually(lambda: s.segments["2"]["translation"])
    release.set()
    eventually(lambda: s.segments["1"]["translation"])
    s.segment("1", "wrong duplicate final", True, "system", 0, 3)
    state = app.call("captions.state")
    assert [(v["id"], v["seq"], v["text"]) for v in state["segments"]] == [("1", 1, "first"), ("2", 2, "second")]
    assert state["segments"][0]["translation"] == "first translated"
    revisions = [event["revision"] for event in app.events]
    assert revisions == sorted(set(revisions))


def test_original_mode_does_not_call_translation_provider(app):
    app.call("captions.start", {"display_mode": "original"})
    s = app.live_holder["session"]
    s.segment("one", "Hello", True, "system", 0, 1)
    assert not app.calls and not s.segments["one"]["translation"]
    app.call("captions.configure", {"display_mode": "bilingual"})
    eventually(lambda: s.segments["one"]["translation"])
    assert len(app.calls) == 1


def test_stop_restart_ignores_old_translation_and_final(app):
    entered, release = threading.Event(), threading.Event()
    def chat(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return "stale translation"
    app.providers.chat = chat
    app.call("captions.start")
    old = app.live_holder["session"]
    old.segment("same", "old", True, "system", 0, 1)
    assert entered.wait(2)
    stopped = app.call("captions.stop")
    assert not stopped["active"]
    new = app.call("captions.start", {"display_mode": "original"})
    new_session = app.live_holder["session"]
    new_session.segment("same", "new", True, "system", 0, 1)
    event_count = len(app.events)
    old.segment("late", "late old final", True, "system", 1, 2)
    release.set()
    old.translate_pool.shutdown(wait=True)
    assert len(app.events) == event_count
    state = app.call("captions.state")
    assert state["session_id"] == new["session_id"] != old.id
    assert state["segments"][0]["text"] == "new"
    assert state["segments"][0]["translation"] == ""


def test_meeting_and_captions_cannot_preempt_each_other(app):
    app.call("live.start", {"record_audio": False})
    meeting = app.live_holder["session"]
    with pytest.raises(ValueError, match="实时助手正在运行"):
        app.call("captions.start")
    assert not meeting.stop_event.is_set()
    app.call("captions.stop")
    assert not meeting.stop_event.is_set()
    app.call("live.stop")
    app.call("captions.start")
    captions = app.live_holder["session"]
    with pytest.raises(ValueError, match="实时字幕正在运行"):
        app.call("live.start")
    with pytest.raises(ValueError):
        app.call("live.stop")
    assert not captions.stop_event.is_set()


def test_translation_failure_keeps_original_and_reports_error(app):
    def fail(*args, **kwargs):
        raise ValueError("fixture service unavailable")
    app.providers.chat = fail
    app.call("captions.start")
    s = app.live_holder["session"]
    s.segment("one", "Still readable", True, "system", 0, 1)
    eventually(lambda: app.call("captions.state")["status"] == "translation_error")
    state = app.call("captions.state")
    assert state["active"] and state["segments"][0]["text"] == "Still readable"
    assert "翻译失败" in state["message"]
    app.providers.chat = lambda *args, **kwargs: "服务已恢复"
    s.segment("two", "Recovered", True, "system", 1, 2)
    eventually(lambda: app.call("captions.state")["status"] == "listening")
    s.status("reconnecting", "识别网络正在重连")
    s.segment("three", "Do not mask ASR status", True, "system", 2, 3)
    eventually(lambda: s.segments["three"]["translation"])
    assert app.call("captions.state")["status"] == "reconnecting"


def test_missing_key_fails_start_without_orphan_session(app, monkeypatch):
    monkeypatch.setattr(app.settings, "secret", lambda _: "")
    with pytest.raises(ValueError, match="API Key"):
        app.call("captions.start")
    state = app.call("captions.state")
    assert not state["active"] and state["status"] == "error"
    assert not app.captures
    assert not (app.data_dir / "sessions").exists()


def test_configuration_persists_and_invalid_values_are_rejected(app):
    state = app.call("captions.configure", {"font_size": 40, "background_opacity": .4, "display_mode": "original", "system_id": "fixture"})
    assert state["options"]["font_size"] == 40
    assert Settings(app.data_dir).get()["preferences"]["captions"] == state["options"]
    for invalid in ({"font_size": 0}, {"background_opacity": 2}, {"font_size": float("nan")}, {"display_mode": "fake"}):
        with pytest.raises(ValueError):
            app.call("captions.configure", invalid)
    assert app.call("captions.state")["options"] == state["options"]
    assert app.call("captions.start")["options"] == state["options"]
    with pytest.raises(ValueError, match="停止字幕"):
        app.call("captions.configure", {"system_id": "different"})
    assert app.call("captions.state")["options"]["system_id"] == "fixture"


def test_local_preset_routes_capture_to_local_asr(app):
    app.settings.update({"preferences": {"model_mode": "local", "asr_model": "faster-whisper-small", "asr_engine": "faster-whisper", "asr_device": "cpu", "asr_compute_type": "int8"}})
    app.call("captions.start", {"engine": "api", "model": "stale-cloud"})
    s = app.live_holder["session"]
    assert app.captures == [("system", False, LocalASR)]
    assert s.params["model"] == "faster-whisper-small" and s.params["device"] == "cpu"
    assert s.params["compute_type"] == "int8"
    assert app.jobs.gpu_lock.locked()
    app.call("captions.stop")
    assert not app.jobs.gpu_lock.locked()


def test_caption_storage_and_translation_queue_are_bounded(app):
    entered, release = threading.Event(), threading.Event()
    def chat(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return "译文"
    app.providers.chat = chat
    app.call("captions.start")
    s = app.live_holder["session"]
    for n in range(45):
        s.segment(str(n), str(n), True, "system", n, n + 1)
    assert entered.wait(2)
    assert len(s.segments) == 30
    assert s.translate_pool._work_queue.qsize() <= 2
    assert app.call("captions.state")["segments"][0]["seq"] == 16
    release.set()
    s.translate_pool.shutdown(wait=True)
    assert len(s.segments) == 30  # Late translations never reinsert evicted segments.


def test_capture_failure_shuts_down_and_is_actionable(app):
    app.call("captions.start")
    s = app.live_holder["session"]
    s.fail("系统声音设备已断开，请重新选择设备")
    eventually(lambda: s.metadata.get("ended_at"))
    state = app.call("captions.state")
    assert not state["active"] and state["status"] == "error"
    assert "重新选择设备" in state["message"]


def test_partial_is_hidden_until_stable_prefix_then_final_only_appends_tail(app, monkeypatch):
    app.call('captions.start')
    s = app.live_holder['session']
    clock = [0.0]
    monkeypatch.setattr('toolbox.live.time.monotonic', lambda: clock[0])
    for now, text in ((0, 'Good sometimes'), (.6, 'Good subtitles are stable'), (1.9, 'Good subtitles are stable. The next sentence')):
        clock[0] = now
        s.segment('one', text, False, 'system', 0, now)
        if now < 1.9:
            assert not s.segments and not app.calls
    assert list(s.segments) == ['one']
    assert s.segments['one']['text'] == 'Good subtitles are stable.'
    assert s.segments['one']['final'] and not s.segments['one']['recognition_final']
    clock[0] = 4
    s.segment('one', 'Good subtitles are stable. The next sentence ends.', True, 'system', 0, 4)
    s.translate_pool.shutdown(wait=True)
    assert [record['text'] for record in s.segments.values()] == ['Good subtitles are stable.', 'The next sentence ends.']
    assert len(app.calls) == 2
    assert all(record['translation_state'] == 'ready' and record['translation_final'] for record in s.segments.values())
    assert not any(event['type'] == 'captions.segment' and not event['final'] for event in app.events)


def test_each_final_cue_keeps_its_own_translation_when_requests_finish_out_of_order(app):
    entered, release = threading.Event(), threading.Event()
    def chat(messages, **kwargs):
        text = messages[-1]['content']
        if text == 'First sentence.':
            entered.set()
            assert release.wait(2)
        return '译文：' + text
    app.providers.chat = chat
    app.call('captions.start')
    s = app.live_holder['session']
    s.segment('utterance', 'First sentence. Second sentence.', True, 'system', 0, 4)
    assert entered.wait(2)
    eventually(lambda: s.segments['utterance:1']['translation_state'] == 'ready')
    assert s.segments['utterance']['translation_state'] == 'pending'
    release.set()
    s.translate_pool.shutdown(wait=True)
    for record in s.segments.values():
        assert record['translation'] == '译文：' + record['text']
        assert 2000 <= record['duration_ms'] <= 4500
    assert [record['seq'] for record in s.segments.values()] == [1, 2]


def test_target_language_change_discards_late_translation_for_previous_language(app):
    entered, release = threading.Event(), threading.Event()
    def chat(messages, **kwargs):
        if '简体中文' in messages[0]['content']:
            entered.set()
            assert release.wait(2)
            return '旧中文译文'
        return 'nouvelle traduction'
    app.providers.chat = chat
    app.call('captions.start', {'target_language': 'zh'})
    s = app.live_holder['session']
    s.segment('one', 'Hello world.', True, 'system', 0, 2)
    assert entered.wait(2)
    app.call('captions.configure', {'target_language': 'fr'})
    eventually(lambda: s.segments['one']['translation'] == 'nouvelle traduction')
    release.set()
    s.translate_pool.shutdown(wait=True)
    assert s.segments['one']['translation'] == 'nouvelle traduction'
    assert not any(event.get('translation') == '旧中文译文' for event in app.events)
    assert s.segments['one']['translation_state'] == 'ready'


def test_source_language_persists_routes_local_and_cannot_change_while_active(app):
    app.settings.update({'preferences': {'model_mode': 'local', 'asr_engine': 'faster-whisper', 'asr_model': 'faster-whisper-small'}})
    configured = app.call('captions.configure', {'source_language': 'en', 'target_language': 'zh'})
    assert configured['options']['source_language'] == 'en'
    app.call('captions.start')
    s = app.live_holder['session']
    assert s.params['source_language'] == 'en' and s.params['language'] == 'en'
    with pytest.raises(ValueError, match='停止字幕'):
        app.call('captions.configure', {'source_language': 'ja'})
    assert s.params['language'] == 'en'
    app.call('captions.stop')
    assert app.call('captions.configure', {'source_language': 'auto'})['options']['source_language'] == 'auto'
    with pytest.raises(ValueError, match='识别语言'):
        app.call('captions.configure', {'source_language': 'invalid'})

