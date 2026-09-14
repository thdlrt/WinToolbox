"""Mode setup and isolation tests; all state belongs to temporary test data."""
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from toolbox.app import App
from toolbox.providers import ProviderError
from toolbox.setup import PRESETS, recommended_preset


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("toolbox.settings.protect", lambda value, decrypt=False: value.removeprefix("fixture:") if decrypt else "fixture:" + value)
    monkeypatch.setattr("toolbox.setup.detect_hardware", lambda: {"cpu": "fixture CPU", "ram_gb": 16, "gpus": [], "vram_gb": None, "note": "fixture"})
    instance = App(tmp_path / "data", register_features=False, register_live=False)
    yield instance
    instance.close()


def test_hardware_recommendation_does_not_assume_large_ram_means_gpu():
    assert recommended_preset({"ram_gb": 64}) == "light"
    assert recommended_preset({"ram_gb": 16, "vram_gb": 8}) == "balanced"
    assert recommended_preset({"ram_gb": 32, "vram_gb": 16}) == "quality"
    assert recommended_preset({}) == "light"


def test_setup_get_is_read_only_and_never_exposes_key(app):
    state = app.call("setup.get")
    assert state["mode"] == "bailian" and state["preset"] == "light"
    assert len(state["presets"]) == 3 and not any(p["installed"] for p in state["presets"])
    assert state["has_key"] is False and not app.settings.path.exists()
    app.call("setup.apply", {"mode": "bailian", "api_key": "secret-fixture"})
    assert "secret-fixture" not in json.dumps(app.call("setup.get"))
    assert app.call("setup.get")["has_key"]


def test_bailian_applies_roles_and_preserves_other_provider_secrets(app):
    app.settings.update({"providers": [{"id": "other", "name": "Existing", "kind": "openai", "base_url": "https://example.invalid/v1", "api_key": "other-fixture"}]})
    result = app.call("setup.apply", {"mode": "bailian", "region": "intl", "api_key": "bailian-fixture"})
    assert result["preferences"]["asr_engine"] == "api"
    assert result["roles"]["chat"] == {"provider_id": "dashscope", "model": "qwen3.8-flash"}
    assert app.settings.secret("other") == "other-fixture"
    assert next(p for p in result["providers"] if p["id"] == "dashscope")["base_url"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    app.call("setup.apply", {"mode": "local", "preset": "light"})
    app.call("setup.apply", {"mode": "bailian", "api_key": ""})
    assert app.settings.secret("dashscope") == "bailian-fixture"
    assert not app.local_llm._closed.is_set()
    assert app.providers.local_llm is app.local_llm


def test_legacy_bailian_key_is_reused_without_reentry(app):
    app.settings.update({"providers": [{"id": "old-bailian", "name": "百炼", "kind": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "region": "cn", "api_key": "legacy-fixture"}]})
    assert app.call("setup.get")["has_key"]
    assert app.call("setup.get")["provider_id"] == "old-bailian"
    result = app.call("setup.apply", {"mode": "bailian", "api_key": ""})
    assert result["roles"]["chat"]["provider_id"] == "old-bailian"
    assert app.settings.secret("old-bailian") == "legacy-fixture"
    app.call("setup.apply", {"mode": "local", "preset": "light"})
    app.call("setup.apply", {"mode": "bailian"})
    assert app.settings.secret("old-bailian") == "legacy-fixture"


@pytest.mark.parametrize("preset", PRESETS, ids=lambda preset: preset["id"])
def test_local_profiles_set_asr_and_all_roles(app, preset):
    result = app.call("setup.apply", {"mode": "local", "preset": preset["id"]})
    preferences = result["preferences"]
    for key in ("asr_engine", "asr_model", "asr_device", "asr_compute_type"):
        assert preferences[key] == preset[key]
    assert preferences["local_preset"] == preset["id"]
    assert result["roles"]["chat"]["model"] == preset["llm_model"]
    assert all(role["provider_id"] == "local" for role in result["roles"].values())
    assert result["roles"]["embedding"]["model"] == "embeddinggemma:latest"


def test_invalid_apply_leaves_state_unchanged(app):
    before = app.settings.get()
    for params in ({"mode": "unknown"}, {"mode": "local", "preset": "giant"}, {"mode": "bailian"}, {"mode": "bailian", "region": "bad", "api_key": "fixture"}):
        with pytest.raises(ValueError):
            app.call("setup.apply", params)
        assert app.settings.get() == before


def test_switch_refuses_active_jobs_and_live_session(app):
    before = app.settings.get()
    with app.jobs.lock:
        app.jobs.active["fixture"] = object()
    try:
        with pytest.raises(RuntimeError, match="任务"):
            app.call("setup.apply", {"mode": "local", "preset": "light"})
    finally:
        app.jobs.active.clear()
    app.live_holder = {"session": SimpleNamespace(stop_event=threading.Event())}
    try:
        with pytest.raises(RuntimeError, match="实时会话"):
            app.call("setup.apply", {"mode": "local", "preset": "light"})
    finally:
        app.live_holder.clear()
    assert app.settings.get() == before


def test_install_uses_one_job_and_does_not_apply_mode(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.models, "install", lambda job: calls.append(dict(job.params)))
    before = app.settings.get()
    record = app.call("setup.install", {"preset": "light"})
    deadline = time.monotonic() + 5
    while app.jobs.active and time.monotonic() < deadline:
        time.sleep(.01)
    result = app.jobs.get(record["id"])
    assert record["tool"] == "setup.install" and result["status"] == "completed", result
    assert [call["model_id"] for call in calls] == PRESETS[0]["model_ids"]
    assert calls[0]["cpu_only"] is True
    assert app.settings.get() == before


def test_local_inference_uses_runtime_and_never_cloud_even_on_failure(app, monkeypatch):
    app.call("setup.apply", {"mode": "local", "preset": "light"})
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(500)))
    app.providers.client.close()
    app.providers.client = client
    calls = []
    monkeypatch.setattr(app.local_llm, "chat", lambda messages, **kwargs: calls.append(kwargs) or "local answer")
    monkeypatch.setattr(app.local_llm, "embed", lambda texts, **kwargs: [[1.0, 0.0] for _ in texts])
    assert app.providers.chat([{"role": "user", "content": "hello"}], role="vision") == "local answer"
    assert calls[-1]["model"] == "qwen3.5:0.8b"
    assert app.providers.embed(["hello"]) == [[1.0, 0.0]]
    with pytest.raises(ProviderError, match="云端"):
        app.providers.chat([], provider_id="dashscope")
    with pytest.raises(ProviderError, match="本地"):
        app.providers.transcribe("does-not-exist.wav", duration=1)
    with pytest.raises(ProviderError, match="本地"):
        app.providers.tts("hello", "does-not-exist.wav")
    def fail(*args, **kwargs):
        raise RuntimeError("local runtime missing")
    monkeypatch.setattr(app.local_llm, "chat", fail)
    with pytest.raises(RuntimeError, match="missing"):
        app.providers.chat([])
    assert not requests


def test_restored_runtime_reference_is_fresh(app):
    old_runtime = app.local_llm
    app.before_restore("test")
    assert old_runtime._closed.is_set()
    app.after_restore()
    assert app.local_llm is not old_runtime
    assert not app.local_llm._closed.is_set()
    assert app.providers.local_llm is app.local_llm
