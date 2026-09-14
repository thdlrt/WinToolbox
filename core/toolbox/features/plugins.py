"""Versioned local tool packages. Process isolation is not a security sandbox."""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from ._common import child_path, extract_zip, write_json


def validate_manifest(manifest, directory):
    if not isinstance(manifest, dict):
        raise ValueError("工具包清单必须是对象")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", str(manifest.get("id", ""))):
        raise ValueError("工具包 ID 只能使用小写字母、数字和连字符")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise ValueError("工具包缺少名称")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?", str(manifest.get("version", ""))):
        raise ValueError("工具包需要有效的三段版本号")
    if manifest.get("api_version") != 1 or manifest.get("runtime") != "python":
        raise ValueError("此宿主仅支持 API 1 和 Python 工具包")
    entry = child_path(directory, manifest.get("entrypoint", ""))
    if entry.suffix != ".py" or not entry.is_file():
        raise ValueError("工具包缺少 Python 入口文件")
    permissions = manifest.get("permissions", [])
    if not isinstance(permissions, list) or any(p not in ("filesystem", "network", "subprocess") for p in permissions):
        raise ValueError("未知的工具权限声明")
    fields = manifest.get("ui", {}).get("fields", [])
    if not isinstance(fields, list) or len(fields) > 40:
        raise ValueError("工具界面字段无效")
    names = set()
    for field in fields:
        if not isinstance(field, dict) or not re.fullmatch(r"[a-zA-Z][\w-]*", str(field.get("name", ""))):
            raise ValueError("工具界面字段缺少有效 name")
        if field["name"] in names or field.get("type", "text") not in ("text", "textarea", "number", "boolean", "select", "file", "directory"):
            raise ValueError("工具界面字段重复或类型不受支持")
        names.add(field["name"])
    return manifest


def register(app):
    root = app.data_dir / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()
    running = set()

    def get(plugin_id):
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", str(plugin_id)):
            raise ValueError("无效工具包 ID")
        directory = root / plugin_id
        if not directory.is_dir():
            raise ValueError("工具包未安装")
        manifest = validate_manifest(json.loads((directory / "manifest.json").read_text(encoding="utf-8")), directory)
        state_file = directory / ".state.json"
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"enabled": True}
        return directory, {**manifest, "enabled": state["enabled"], "isolation": "process", "sandboxed": False}

    def listing(params):
        items = []
        for directory in sorted(root.iterdir()):
            if directory.is_dir() and not directory.name.startswith("."):
                try:
                    items.append(get(directory.name)[1])
                except Exception as exc:
                    items.append({"id": directory.name, "name": directory.name, "enabled": False, "error": str(exc)})
        return {"plugins": items}

    def install(params):
        package = Path(params["path"]).resolve()
        if package.suffix.lower() != ".toolpkg":
            raise ValueError("请选择 .toolpkg 文件")
        with lock, tempfile.TemporaryDirectory(prefix=".install-", dir=root) as staging:
            with zipfile.ZipFile(package) as archive:
                extract_zip(archive, staging, max_bytes=256 * 1024**2, max_files=2000)
            manifest = validate_manifest(json.loads((Path(staging) / "manifest.json").read_text(encoding="utf-8")), staging)
            plugin_id = manifest["id"]
            if plugin_id in running:
                raise ValueError("工具正在运行，请结束任务后升级")
            destination = root / plugin_id
            old = root / (".old-" + uuid.uuid4().hex)
            enabled = get(plugin_id)[1]["enabled"] if destination.exists() else True
            write_json(Path(staging) / ".state.json", {"enabled": enabled, "installed_at": time.time()})
            if destination.exists():
                os.replace(destination, old)
            try:
                os.replace(staging, destination)
            except BaseException:
                if old.exists():
                    os.replace(old, destination)
                raise
            if old.exists():
                shutil.rmtree(old)
            return {"plugin": get(plugin_id)[1]}

    def enable(params):
        with lock:
            directory, _ = get(params["id"])
            write_json(directory / ".state.json", {"enabled": bool(params["enabled"])})
            return {"plugin": get(params["id"])[1]}

    def uninstall(params):
        with lock:
            directory, _ = get(params["id"])
            if params["id"] in running:
                raise ValueError("工具正在运行，请结束任务后卸载")
            shutil.rmtree(directory)
            return {"ok": True}

    def run_job(job):
        plugin_id = job.params["id"]
        with lock:
            directory, manifest = get(plugin_id)
            if not manifest["enabled"]:
                raise ValueError("请先启用工具包")
            if plugin_id in running:
                raise ValueError("此工具已有运行中的任务")
            running.add(plugin_id)
        output_dir = app.data_dir / "artifacts" / job.id
        output_dir.mkdir(parents=True, exist_ok=True)
        process = None
        reader_stop = threading.Event()
        try:
            log_path = output_dir / "plugin.stderr.log"
            with log_path.open("w", encoding="utf-8") as stderr:
                process = subprocess.Popen([sys.executable, "-I", "-u", str(directory / manifest["entrypoint"])],
                    cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                    text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                process.stdin.write(json.dumps({"params": job.params.get("params", {}), "context": {"output_dir": str(output_dir), "api_version": 1}}, ensure_ascii=False) + "\n")
                process.stdin.close()
                messages = queue.Queue(maxsize=1000)
                def read_output():
                    def enqueue(value):
                        while not reader_stop.is_set():
                            try:
                                messages.put(value, timeout=0.1)
                                return
                            except queue.Full:
                                pass
                    while not reader_stop.is_set():
                        line = process.stdout.readline(2 * 1024**2 + 1)
                        if not line:
                            break
                        if len(line) > 2 * 1024**2:
                            enqueue(ValueError("工具输出单行超过 2 MB"))
                            break
                        enqueue(line)
                    enqueue(None)
                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                result = None
                started = time.monotonic()
                while True:
                    job.check_cancelled()
                    if time.monotonic() - started > min(int(job.params.get("timeout_seconds", 3600)), 86400):
                        raise TimeoutError("工具执行超时")
                    try:
                        message = messages.get(timeout=0.15)
                    except queue.Empty:
                        continue
                    if message is None:
                        break
                    if isinstance(message, Exception):
                        raise message
                    try:
                        event = json.loads(message)
                    except ValueError as exc:
                        raise ValueError("工具 stdout 必须为 JSON 事件；调试日志请写 stderr") from exc
                    if not isinstance(event, dict):
                        raise ValueError("无效工具事件")
                    if event.get("type") == "progress":
                        job.progress(max(0, min(99, float(event.get("percent", 0)))), str(event.get("message", "")))
                    elif event.get("type") == "result":
                        result = event.get("result")
                    elif event.get("type") == "error":
                        raise ValueError(str(event.get("message", "工具执行失败")))
                exit_code = process.wait(timeout=10)
                if exit_code:
                    raise ValueError(f"工具退出码 {exit_code}，请查看日志 {log_path}")
                if result is None:
                    raise ValueError("工具没有返回 result 事件")
                result_path = output_dir / "result.json"
                write_json(result_path, result)
                job.artifact(result_path, "json", "工具运行结果")
                if log_path.stat().st_size:
                    job.artifact(log_path, "log", "工具日志")
                return result
        finally:
            reader_stop.set()
            if process and process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    process.kill()
                process.wait(timeout=10)
            if process and process.stdout:
                process.stdout.close()
            with lock:
                running.discard(plugin_id)

    app.jobs.register("plugin", run_job)
    app.register("plugins.list", listing)
    app.register("plugins.install", install)
    app.register("plugins.enable", enable)
    app.register("plugins.uninstall", uninstall)
    app.register("plugins.run", lambda params: app.jobs.submit("plugin", params, run_job))
