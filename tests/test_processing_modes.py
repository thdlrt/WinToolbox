"""Regression checks for stale page state crossing the cloud/local boundary."""
import threading
import time
import wave
from pathlib import Path

import pytest

from toolbox.app import App
from toolbox.live import LiveSession, LocalASR, _role


@pytest.fixture
def app(tmp_path):
    instance = App(tmp_path / "data", register_features=False, register_live=True)
    instance.settings.update({"preferences": {
        "model_mode": "local", "local_preset": "light", "asr_engine": "faster-whisper",
        "asr_model": "faster-whisper-small", "asr_device": "cpu", "asr_compute_type": "int8",
    }})
    yield instance
    instance.close()


def wait_job(app, job):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        saved = app.jobs.get(job["id"])
        if saved["status"] not in ("queued", "running"):
            assert saved["status"] == "completed", saved
            return saved
        time.sleep(.02)
    pytest.fail("Job did not finish")


def fail_cloud(*args, **kwargs):
    pytest.fail("A local-mode operation reached cloud ASR")


def make_audio(path):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * 16000)


def test_media_submission_ignores_stale_cloud_selection(app, tmp_path, monkeypatch):
    from toolbox import media
    path = tmp_path / "fixture.wav"
    make_audio(path)
    requests = []
    monkeypatch.setattr(app.providers, "transcribe", fail_cloud)
    monkeypatch.setattr(app.models, "require", lambda model, engine=None: {"model": model, "engine": engine})

    def local(app, job, wav, duration, params):
        requests.append(dict(params))
        return [{"start": 0, "end": duration, "text": "Local fixture"}]

    monkeypatch.setattr(media, "transcribe_local", local)
    saved = wait_job(app, app.call("jobs.submit", {"tool": "media", "params": {
        "paths": [str(path)], "engine": "api", "model": "obsolete-cloud-model",
        "translate": False, "tts_provider": "edge",
    }}))
    assert requests[0]["engine"] == "faster-whisper"
    assert requests[0]["model"] == "faster-whisper-small"
    assert requests[0]["device"] == "cpu" and requests[0]["compute_type"] == "int8"
    assert requests[0]["tts_provider"] == "cosyvoice"
    assert any(a["path"].endswith(".srt") for a in saved["artifacts"])


def test_live_stale_api_form_uses_local_preset(app, monkeypatch):
    selected = []
    monkeypatch.setattr(app.models, "require", lambda model, engine=None: selected.append((model, engine)) or {})
    monkeypatch.setattr(LocalASR, "run", lambda self: None)
    monkeypatch.setattr(LiveSession, "capture", lambda *a: None)
    session = LiveSession(app, {"engine": "api", "model": "stale-cloud-model", "record_audio": False})
    try:
        session.start()
        assert selected == [("faster-whisper-small", "faster-whisper")]
        assert session.params["device"] == "cpu" and session.params["compute_type"] == "int8"
        with pytest.raises(ValueError, match="本地模式"):
            _role(app, "live_asr")
    finally:
        session.stop()


def test_worker_receives_preset_compute_type(app, tmp_path, monkeypatch):
    from toolbox import media
    captured = {}
    def worker(app, job, engine, request, model):
        captured.update(request)
        return {"segments": []}
    monkeypatch.setattr(media, "worker", worker)
    media.transcribe_local(app, object(), tmp_path / "fixture.wav", 1, {
        "engine": "faster-whisper", "model": "faster-whisper-small", "device": "cpu", "compute_type": "int8",
    })
    assert captured["device"] == "cpu" and captured["compute_type"] == "int8"


def test_switch_to_bailian_discards_old_local_model(app):
    from toolbox.processing_mode import processing_params
    app.settings.update({"preferences": {"model_mode": "bailian"}})
    actual = processing_params(app, {"engine": "faster-whisper", "model": "faster-whisper-small", "device": "cpu", "compute_type": "int8"})
    assert actual["engine"] == "api" and actual["tts_provider"] == "qwen"
    assert not {"model", "device", "compute_type"}.intersection(actual)


def test_live_answer_can_cancel_before_first_token(app, monkeypatch):
    entered, cancelled = threading.Event(), threading.Event()
    def waiting_model(messages, role="chat", stream_callback=None, cancel=None):
        assert callable(cancel)
        entered.set()
        try:
            while True:
                cancel()
                time.sleep(.01)
        except RuntimeError:
            cancelled.set()
            raise
    monkeypatch.setattr(app.providers, "chat", waiting_model)
    session = LiveSession(app, {"record_audio": False})
    try:
        session.answer("How many samples were used?")
        assert entered.wait(2)
        session.answer_generation += 1
        assert cancelled.wait(2)
        assert not session.answers
    finally:
        session.stop()
