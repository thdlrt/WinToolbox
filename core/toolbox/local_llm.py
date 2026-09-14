"""Private Ollama runtime: explicit installs, local-only inference, no user service.

Protocol and distribution: https://docs.ollama.com/windows and /api/chat.
The four pinned model names below were checked against the official library.
"""
import base64
import hashlib
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .jobs import Cancelled
from .settings import atomic_json


MODEL_SPECS = {
    "qwen3.5:0.8b": {"capabilities": ["completion", "vision"], "context": 8192},
    "qwen3.5:4b": {"capabilities": ["completion", "vision"], "context": 8192},
    "qwen3.5:9b": {"capabilities": ["completion", "vision"], "context": 8192},
    "embeddinggemma:latest": {"capabilities": ["embedding"], "context": 2048},
}
RELEASE_API = "https://api.github.com/repos/ollama/ollama/releases/latest"


class LocalLLM:
    def __init__(self, app):
        self.app = app
        self.root = Path(app.data_dir) / "models" / "ollama"
        self.runtime = Path(app.data_dir) / "runtimes" / "ollama"
        self._process = None
        self._url = None
        self._log = None
        self._start_lock = threading.RLock()
        self._install_lock = threading.Lock()
        self._closed = threading.Event()

    @staticmethod
    def _model(model):
        if model == "embeddinggemma":
            model = "embeddinggemma:latest"
        if model not in MODEL_SPECS:
            raise ValueError("请选择本地预设中的模型；本地模式不支持云端模型或远程模型地址")
        return model

    def _marker(self, model):
        return self.root / "installed" / (model.replace(":", "-") + ".json")

    def _manifest(self, model):
        name, tag = model.split(":", 1)
        return self.root / "manifests" / "registry.ollama.ai" / "library" / name / tag

    def _binary(self):
        managed = self.runtime / "managed" / "ollama.exe"
        if managed.is_file():
            return managed
        existing = shutil.which("ollama")
        if existing:
            return Path(existing)
        if os.name == "nt" and os.getenv("LOCALAPPDATA"):
            candidate = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.is_file():
                return candidate
        return None

    def _manifest_state(self, model):
        """Check small manifest + blob sizes; never load a model or contact a server."""
        path = self._manifest(model)
        try:
            raw = path.read_bytes()
            manifest = json.loads(raw)
            layers = [manifest["config"], *manifest["layers"]]
            if len(layers) < 2:
                return None
            for layer in layers:
                digest, size = layer.get("digest", ""), layer.get("size", 0)
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not isinstance(size, int) or size <= 0:
                    return None
                blob = self.root / "blobs" / digest.replace(":", "-")
                if not blob.is_file() or blob.stat().st_size != size:
                    return None
            return "sha256:" + hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def status(self, model):
        model = self._model(model)
        binary = self._binary()
        digest = self._manifest_state(model)
        try:
            metadata = json.loads(self._marker(model).read_text("utf-8"))
        except (OSError, ValueError):
            metadata = {}
        installed = bool(binary and digest and isinstance(metadata, dict) and metadata.get("digest") == digest and metadata.get("model") == model)
        return {"model": model, "installed": installed, "runtime_installed": bool(binary),
                "running": bool(self._process and self._process.poll() is None),
                "path": str(self.root), "runtime_path": str(binary) if binary else None,
                "digest": digest if installed else None,
                "capabilities": list(MODEL_SPECS[model]["capabilities"])}

    def _check(self, cancel=None):
        if self._closed.is_set():
            raise Cancelled("本地模型服务已关闭")
        if cancel:
            if callable(cancel):
                cancel()
            elif cancel.is_set():
                raise Cancelled("任务已取消")

    def _environment(self, port):
        # Do not inherit a user's cloud auth, server, GPU tuning, proxies or model path.
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("OLLAMA_") and k.upper() not in
               {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
        isolated_home = self.runtime / "home"
        isolated_home.mkdir(parents=True, exist_ok=True)
        env.update(OLLAMA_HOST=f"127.0.0.1:{port}", OLLAMA_MODELS=str(self.root),
                   OLLAMA_NO_CLOUD="1", OLLAMA_KEEP_ALIVE="5m", OLLAMA_NUM_PARALLEL="1",
                   OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_CONTEXT_LENGTH="8192",
                   OLLAMA_ORIGINS="", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
                   HOME=str(isolated_home), USERPROFILE=str(isolated_home))
        return env

    @staticmethod
    def _stop_process(process):
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            # Ollama owns GPU runner children; close this tree, never another Ollama.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _start(self, cancel=None):
        with self._start_lock:
            self._check(cancel)
            if self._process and self._process.poll() is None:
                return
            binary = self._binary()
            if not binary:
                raise RuntimeError("请先在设置 → 本地模式安装一套预设")
            if self._log:
                self._log.close()
            self.root.mkdir(parents=True, exist_ok=True)
            self.runtime.mkdir(parents=True, exist_ok=True)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            self._url = f"http://127.0.0.1:{port}"
            self._log = (self.runtime / "server.log").open("wb")
            self._process = subprocess.Popen([str(binary), "serve"], cwd=str(self.runtime),
                env=self._environment(port), stdin=subprocess.DEVNULL, stdout=self._log,
                stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                deadline = time.monotonic() + 35
                with httpx.Client(timeout=.5, trust_env=False, follow_redirects=False) as client:
                    while time.monotonic() < deadline:
                        self._check(cancel)
                        if self._process.poll() is not None:
                            raise RuntimeError("本地模型服务启动失败，请重新安装本地预设（诊断记录 runtimes/ollama/server.log）")
                        try:
                            response = client.get(self._url + "/api/version")
                            if response.status_code == 200 and response.json().get("version"):
                                # A randomly selected port must belong to our still-live child.
                                time.sleep(.1)
                                if self._process.poll() is None:
                                    return
                        except (httpx.HTTPError, ValueError):
                            pass
                        self._closed.wait(.1)
                raise RuntimeError("本地模型服务启动超时，请查看 runtimes/ollama/server.log")
            except BaseException:
                self._stop_process(self._process)
                self._process = None
                self._url = None
                self._log.close()
                self._log = None
                raise

    @staticmethod
    def _response_error(response):
        if response.status_code >= 400:
            # Keep diagnostics small; the request can contain private documents.
            raise RuntimeError(f"本地模型请求失败（HTTP {response.status_code}），请检查模型安装和可用内存")

    def _events(self, route, payload, cancel=None):
        """Read NDJSON on a worker so cancellation also interrupts a stalled server."""
        self._check(cancel)
        if not self._url or urlsplit(self._url).hostname != "127.0.0.1":
            raise RuntimeError("本地模型服务地址无效")
        mailbox, stopped, active = queue.Queue(), threading.Event(), {}

        def read():
            try:
                with httpx.Client(timeout=httpx.Timeout(600, connect=5), trust_env=False, follow_redirects=False) as client:
                    with client.stream("POST", self._url + route, json=payload) as response:
                        active["response"] = response
                        self._response_error(response)
                        for line in response.iter_lines():
                            if stopped.is_set():
                                break
                            if line.strip():
                                mailbox.put(("data", json.loads(line)))
                mailbox.put(("done", None))
            except Exception as exc:
                mailbox.put(("error", exc))

        worker = threading.Thread(target=read, name="local-llm-http", daemon=True)
        worker.start()
        try:
            while True:
                self._check(cancel)
                try:
                    kind, value = mailbox.get(timeout=.1)
                except queue.Empty:
                    continue
                if kind == "done":
                    return
                if kind == "error":
                    if isinstance(value, httpx.HTTPError):
                        raise RuntimeError("本地模型连接中断，请检查模型服务和可用内存") from value
                    raise value
                if not isinstance(value, dict):
                    raise RuntimeError("本地模型返回格式无效")
                if value.get("error"):
                    raise RuntimeError("本地模型：" + str(value["error"])[:500])
                yield value
        finally:
            stopped.set()
            response = active.get("response")
            if response:
                stream = response.extensions.get("network_stream")
                try:
                    sock = stream.get_extra_info("socket") if stream else None
                    if sock:
                        sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
            worker.join(timeout=.5)

    def _download_runtime(self, job):
        if self._binary():
            return
        if os.name != "nt":
            raise RuntimeError("自动安装本地运行包目前支持 Windows；请先安装 Ollama CLI")
        job.check_cancelled()
        job.progress(2, "下载本地模型运行包")
        with httpx.Client(timeout=60, follow_redirects=True, trust_env=False,
                          headers={"Accept": "application/vnd.github+json"}) as client:
            response = client.get(RELEASE_API)
            response.raise_for_status()
            release = response.json()
            asset = next((a for a in release.get("assets", []) if a["name"] == "ollama-windows-amd64.zip"), None)
            if not asset:
                raise RuntimeError("官方发行版未提供 Windows 本地运行包")
            url = asset["browser_download_url"]
            if not url.startswith("https://github.com/ollama/ollama/releases/download/"):
                raise RuntimeError("本地运行包来源无效")
            expected = asset.get("digest", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected or ""):
                raise RuntimeError("官方运行包缺少 SHA256 校验值，暂未安装，请稍后重试")
            parent = self.runtime.parent
            parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="ollama-install-", dir=parent) as staging:
                staging = Path(staging)
                archive = staging / "runtime.zip"
                digest, count, reported_at = hashlib.sha256(), 0, 0.0
                with client.stream("GET", url, headers={"Accept": "application/octet-stream"}) as download:
                    download.raise_for_status()
                    total = int(download.headers.get("content-length", asset.get("size", 0)) or 0)
                    with archive.open("wb") as output:
                        for chunk in download.iter_bytes(1024 * 1024):
                            job.check_cancelled()
                            output.write(chunk)
                            digest.update(chunk)
                            count += len(chunk)
                            if time.monotonic() - reported_at > .5 or count == total:
                                job.progress(2 + 13 * min(1, count / max(total, 1)), f"下载运行包 {count // 1048576} MB")
                                reported_at = time.monotonic()
                if "sha256:" + digest.hexdigest() != expected:
                    raise RuntimeError("本地运行包 SHA256 校验失败，请重新下载")
                job.check_cancelled()
                unpacked = staging / "unpacked"
                self.app.models.extract(archive, unpacked)
                if not (unpacked / "ollama.exe").is_file():
                    raise RuntimeError("本地运行包缺少 ollama.exe")
                atomic_json(unpacked / "runtime.json", {"version": release["tag_name"], "sha256": expected, "source": url})
                # Publish the full checked runtime atomically; a cancelled download or
                # failed extraction must never leave a usable-looking partial binary.
                job.check_cancelled()
                self.runtime.mkdir(parents=True, exist_ok=True)
                destination = self.runtime / "managed"
                if destination.exists():
                    raise RuntimeError("本地运行包目录不完整，请修复后重试")
                unpacked.rename(destination)

    def install(self, job, model):
        model = self._model(model)
        while not self._install_lock.acquire(timeout=.1):
            self._check(job.check_cancelled)
        try:
            self._check(job.check_cancelled)
            if self.status(model)["installed"]:
                return self.status(model)
            self._download_runtime(job)
            self._start(job.check_cancelled)
            success = False
            for event in self._events("/api/pull", {"model": model, "stream": True}, job.check_cancelled):
                total, completed = event.get("total", 0), event.get("completed", 0)
                percent = 18 + 74 * min(1, completed / total) if total else 18
                job.progress(percent, f"安装 {model} · {event.get('status', '下载中')}")
                success = success or event.get("status") == "success"
            if not success:
                raise RuntimeError("模型下载未完整结束，请重试安装")
            self._check(job.check_cancelled)
            info = list(self._events("/api/show", {"model": model}, job.check_cancelled))
            capabilities = info[-1].get("capabilities", []) if info else []
            required = MODEL_SPECS[model]["capabilities"]
            if any(cap not in capabilities for cap in required):
                raise RuntimeError("本地运行包未能识别模型能力，请更新 Ollama 运行包后重试")
            digest = self._manifest_state(model)
            if not digest:
                raise RuntimeError("下载结束但本地模型文件不完整，请重试安装")
            self._check(job.check_cancelled)
            atomic_json(self._marker(model), {"model": model, "digest": digest, "capabilities": capabilities})
            job.artifact(self._marker(model), "model", model)
            job.progress(100, f"{model} 已安装")
            return self.status(model)
        finally:
            self._install_lock.release()

    def _require(self, model):
        model = self._model(model)
        if not self.status(model)["installed"]:
            raise RuntimeError(f"请先在设置 → 本地模式安装 {model}；本地模式不会使用云端模型")
        return model

    @staticmethod
    def _messages(messages):
        result = []
        for message in messages:
            role = message.get("role")
            if role not in ("system", "user", "assistant"):
                raise ValueError("本地模型消息角色无效")
            content = message.get("content", "")
            item = {"role": role, "content": ""}
            if isinstance(content, str):
                item["content"] = content
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        item["content"] += part.get("text", "")
                    elif part.get("type") == "image_url":
                        image = part.get("image_url", {})
                        url = image if isinstance(image, str) else image.get("url", "")
                        if not re.match(r"^data:image/(?:png|jpeg|jpg|webp|gif);base64,", url):
                            raise ValueError("本地视觉仅接受文件图片，不支持网络图片地址")
                        data = url.split(",", 1)[1]
                        try:
                            base64.b64decode(data, validate=True)
                        except (ValueError, TypeError) as exc:
                            raise ValueError("本地图片编码无效") from exc
                        item.setdefault("images", []).append(data)
                    else:
                        raise ValueError("本地模型不支持此消息内容类型")
            else:
                raise ValueError("本地模型消息内容无效")
            result.append(item)
        return result

    def chat(self, messages, model, stream_callback=None, cancel=None):
        self._check(cancel)
        model = self._require(model)
        if "completion" not in MODEL_SPECS[model]["capabilities"]:
            raise ValueError("请选择本地问答模型")
        payload = {"model": model, "messages": self._messages(messages), "stream": True, "think": False,
                   "keep_alive": "5m", "options": {"num_ctx": MODEL_SPECS[model]["context"], "num_predict": 4096, "temperature": 0.2, "presence_penalty": 0}}
        self._start(cancel)
        parts, done = [], False
        for event in self._events("/api/chat", payload, cancel):
            text = event.get("message", {}).get("content", "")
            if text:
                parts.append(text)
                if stream_callback:
                    stream_callback(text)
            if event.get("done"):
                if event.get("done_reason") == "length":
                    raise RuntimeError("本地模型输出达到长度上限，请缩短内容分段")
                done = True
        self._check(cancel)
        if not done:
            raise RuntimeError("本地模型回复中断，请重试")
        result = "".join(parts)
        if not result.strip():
            raise RuntimeError("本地模型未返回文字，请缩短问题后重试")
        return result

    def embed(self, texts, model):
        if not texts:
            return []
        model = self._require(model)
        if "embedding" not in MODEL_SPECS[model]["capabilities"]:
            raise ValueError("请选择本地资料检索模型")
        self._start()
        result = []
        # Bound peak memory and expose context overflow instead of silently truncating.
        for offset in range(0, len(texts), 16):
            batch = list(texts[offset:offset + 16])
            if any(not isinstance(text, str) for text in batch):
                raise ValueError("资料检索输入必须是文本列表")
            events = list(self._events("/api/embed", {"model": model, "input": batch, "truncate": False, "keep_alive": "5m"}))
            vectors = events[-1].get("embeddings", []) if events else []
            if len(vectors) != len(batch) or any(not isinstance(v, list) or not v or any(
                    not isinstance(n, (int, float)) or not math.isfinite(n) for n in v) for v in vectors):
                raise RuntimeError("本地检索模型返回的向量无效")
            if any(len(v) != len(vectors[0]) for v in vectors) or result and len(result[0]) != len(vectors[0]):
                raise RuntimeError("本地检索模型向量维度不一致")
            result.extend(vectors)
        return result

    def close(self):
        self._closed.set()
        with self._start_lock:
            self._stop_process(self._process)
            self._process = None
            self._url = None
            if self._log:
                self._log.close()
                self._log = None
