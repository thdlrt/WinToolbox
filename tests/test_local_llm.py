import base64
import hashlib
import io
import json
import os
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from toolbox.jobs import Cancelled
from toolbox.local_llm import LocalLLM
from toolbox.models import Models
from toolbox.settings import atomic_json


class Job:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.progress_events = []
        self.artifacts = []

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise Cancelled("cancelled")

    def progress(self, percent, message):
        self.check_cancelled()
        self.progress_events.append((percent, message))

    def artifact(self, path, *args):
        self.artifacts.append(path)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    app = SimpleNamespace(data_dir=tmp_path / "data", models=SimpleNamespace(extract=Models.extract))
    instance = LocalLLM(app)
    binary = tmp_path / "fake-ollama.exe"
    binary.write_bytes(b"test binary")
    monkeypatch.setattr(instance, "_binary", lambda: binary)
    yield instance
    instance.close()


def manifest(runtime, model, *, installed=True):
    parts = []
    for content in (b"fixture config", b"fixture model"):
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        blob = runtime.root / "blobs" / digest.replace(":", "-")
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)
        parts.append({"digest": digest, "size": len(content)})
    atomic_json(runtime._manifest(model), {"config": parts[0], "layers": parts[1:]})
    if installed:
        atomic_json(runtime._marker(model), {"model": model, "digest": runtime._manifest_state(model)})


@pytest.fixture
def endpoint(runtime, monkeypatch):
    state = SimpleNamespace(requests=[], routes={}, entered=threading.Event(), release=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((self.path, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            try:
                for event in state.routes[self.path](body):
                    self.wfile.write(json.dumps(event).encode() + b"\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime._url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(runtime, "_start", lambda cancel=None: runtime._check(cancel))
    yield state
    state.release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_status_never_starts_or_downloads_and_detects_missing_blob(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_start", lambda *a: pytest.fail("status started a service"))
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **kw: pytest.fail("status contacted a service"))
    assert runtime.status("qwen3.5:0.8b")["installed"] is False
    manifest(runtime, "qwen3.5:0.8b")
    assert runtime.status("qwen3.5:0.8b")["installed"] is True
    next((runtime.root / "blobs").iterdir()).unlink()
    assert runtime.status("qwen3.5:0.8b")["installed"] is False


def test_uninstalled_inference_never_contacts_network(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_start", lambda *a: pytest.fail("uninstalled model started a service"))
    with pytest.raises(RuntimeError, match="先.*安装"):
        runtime.chat([{"role": "user", "content": "hello"}], "qwen3.5:0.8b")
    with pytest.raises(ValueError, match="云端"):
        runtime.chat([], "qwen3.5:cloud")


def test_private_environment_discards_user_service_cloud_and_proxy(runtime, monkeypatch):
    for key, value in {"OLLAMA_HOST": "https://user.example", "OLLAMA_MODELS": "C:/private-models",
                       "HTTPS_PROXY": "https://proxy.invalid", "http_proxy": "http://proxy.invalid",
                       "OLLAMA_API_KEY": "secret"}.items():
        monkeypatch.setenv(key, value)
    env = runtime._environment(20001)
    assert env["OLLAMA_HOST"] == "127.0.0.1:20001"
    assert env["OLLAMA_MODELS"] == str(runtime.root)
    assert env["OLLAMA_NO_CLOUD"] == "1"
    assert env["USERPROFILE"] == str(runtime.runtime / "home")
    assert not any(k.upper() in ("HTTPS_PROXY", "HTTP_PROXY", "OLLAMA_API_KEY") for k in env)


def test_lazy_process_uses_private_port_and_close_stops_only_its_tree(runtime, monkeypatch):
    launches, stops = [], []
    class Process:
        pid = 123456
        stopped = False
        def poll(self):
            return 0 if self.stopped else None
        def wait(self, **kwargs):
            self.stopped = True
        def terminate(self):
            self.stopped = True
    process = Process()
    def launch(args, **kwargs):
        launches.append((args, kwargs))
        return process
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: stops.append(args))
    real_client = httpx.Client
    def request(req):
        assert req.url.host == "127.0.0.1" and req.url.port != 11434
        return httpx.Response(200, json={"version": "0.99.0"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(request), **kwargs))
    assert not launches
    runtime.status("qwen3.5:0.8b")
    assert not launches
    runtime._start()
    runtime._start()
    assert len(launches) == 1 and launches[0][0][-1] == "serve"
    assert launches[0][1]["env"]["OLLAMA_MODELS"] == str(runtime.root)
    assert launches[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    runtime.close()
    assert process.stopped
    if os.name == "nt":
        assert stops == [["taskkill", "/PID", "123456", "/T", "/F"]]
    with pytest.raises(Cancelled):
        runtime._start()


def test_chat_stream_images_and_proxy_bypass(runtime, endpoint, monkeypatch):
    manifest(runtime, "qwen3.5:0.8b")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    endpoint.routes["/api/chat"] = lambda payload: [
        {"message": {"content": "你好"}, "done": False},
        {"message": {"content": "。"}, "done": True, "done_reason": "stop"}]
    image = base64.b64encode(b"fixture image").decode()
    chunks = []
    result = runtime.chat([{"role": "user", "content": [
        {"type": "text", "text": "描述图片"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}}]}],
        "qwen3.5:0.8b", stream_callback=chunks.append)
    assert result == "你好。" and chunks == ["你好", "。"]
    path, request = endpoint.requests[0]
    assert path == "/api/chat" and request["think"] is False
    assert request["messages"] == [{"role": "user", "content": "描述图片", "images": [image]}]
    with pytest.raises(ValueError, match="网络图片"):
        runtime.chat([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://private.example/photo.png"}}]}], "qwen3.5:0.8b")
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("event, message", [
    ({"message": {"content": "partial"}, "done": False}, "中断"),
    ({"message": {"content": "partial"}, "done": True, "done_reason": "length"}, "长度上限"),
    ({"error": "out of memory"}, "out of memory"),
])
def test_chat_rejects_partial_error_or_truncated_output(runtime, endpoint, event, message):
    manifest(runtime, "qwen3.5:0.8b")
    endpoint.routes["/api/chat"] = lambda payload: [event]
    with pytest.raises(RuntimeError, match=message):
        runtime.chat([{"role": "user", "content": "hello"}], "qwen3.5:0.8b")


def test_install_requires_pull_success_capability_and_files(runtime, endpoint):
    def pull(payload):
        manifest(runtime, payload["model"], installed=False)
        yield {"status": "pulling", "total": 100, "completed": 30}
        yield {"status": "success"}
    endpoint.routes["/api/pull"] = pull
    endpoint.routes["/api/show"] = lambda p: [{"capabilities": ["completion", "vision"]}]
    job = Job()
    result = runtime.install(job, "qwen3.5:0.8b")
    assert result["installed"] and result["digest"].startswith("sha256:")
    assert job.artifacts and job.progress_events[-1][0] == 100
    count = len(endpoint.requests)
    runtime.install(job, "qwen3.5:0.8b")
    assert len(endpoint.requests) == count  # Already installed: no download/server traffic.


@pytest.mark.parametrize("success, capabilities, files", [
    (False, ["completion", "vision"], True),
    (True, ["completion"], True),
    (True, ["completion", "vision"], False),
])
def test_failed_install_is_not_marked_ready(runtime, endpoint, success, capabilities, files):
    def pull(payload):
        if files:
            manifest(runtime, payload["model"], installed=False)
        yield {"status": "success" if success else "downloading"}
    endpoint.routes["/api/pull"] = pull
    endpoint.routes["/api/show"] = lambda p: [{"capabilities": capabilities}]
    with pytest.raises(RuntimeError):
        runtime.install(Job(), "qwen3.5:0.8b")
    assert not runtime.status("qwen3.5:0.8b")["installed"]


@pytest.mark.parametrize("operation", ["chat", "pull"])
def test_cancel_interrupts_stalled_stream_without_false_ready(runtime, endpoint, operation):
    job = Job()
    def stalled(payload):
        endpoint.entered.set()
        endpoint.release.wait(5)
        yield {"status": "success", "done": True, "message": {"content": "late"}}
    endpoint.routes["/api/" + operation] = stalled
    if operation == "chat":
        manifest(runtime, "qwen3.5:0.8b")
    outcome = []
    def invoke():
        try:
            if operation == "chat":
                runtime.chat([{"role": "user", "content": "hello"}], "qwen3.5:0.8b", cancel=job.check_cancelled)
            else:
                runtime.install(job, "qwen3.5:0.8b")
        except Exception as exc:
            outcome.append(exc)
    thread = threading.Thread(target=invoke)
    thread.start()
    assert endpoint.entered.wait(2)
    start = time.monotonic()
    job.cancel_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive() and time.monotonic() - start < 2
    assert len(outcome) == 1 and isinstance(outcome[0], Cancelled)
    if operation == "pull":
        assert not runtime.status("qwen3.5:0.8b")["installed"]


def test_embed_validates_count_dimensions_and_values(runtime, endpoint):
    manifest(runtime, "embeddinggemma:latest")
    endpoint.routes["/api/embed"] = lambda p: [{"embeddings": [[.1, .2] for _ in p["input"]]}]
    assert runtime.embed(["你好", "hello"], "embeddinggemma:latest") == [[.1, .2], [.1, .2]]
    assert endpoint.requests[0][1]["truncate"] is False
    endpoint.routes["/api/embed"] = lambda p: [{"embeddings": [[float("nan")]]}]
    with pytest.raises(RuntimeError, match="向量无效"):
        runtime.embed(["hello"], "embeddinggemma:latest")


@pytest.mark.skipif(os.name != "nt", reason="Windows standalone distribution")
@pytest.mark.parametrize("unsafe, checksum", [(False, True), (True, True), (False, False)])
def test_runtime_download_checks_hash_and_archive_before_publication(runtime, monkeypatch, unsafe, checksum):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt" if unsafe else "ollama.exe", b"fixture binary")
    data = archive.getvalue()
    digest = "sha256:" + hashlib.sha256(data).hexdigest() if checksum else "sha256:" + "0" * 64
    def handle(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"tag_name": "v1.0.0", "assets": [{"name": "ollama-windows-amd64.zip",
                "digest": digest, "size": len(data), "browser_download_url":
                "https://github.com/ollama/ollama/releases/download/v1.0.0/ollama-windows-amd64.zip"}]})
        return httpx.Response(200, content=data)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(runtime, "_binary", lambda: None)
    if not unsafe and checksum:
        runtime._download_runtime(Job())
        assert (runtime.runtime / "managed" / "ollama.exe").read_bytes() == b"fixture binary"
    else:
        with pytest.raises((RuntimeError, ValueError)):
            runtime._download_runtime(Job())
        assert not (runtime.runtime / "managed").exists()
        assert not (runtime.runtime.parent / "escape.txt").exists()
