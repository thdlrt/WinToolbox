import copy
import contextlib
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


class Cancelled(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def file_signature(path):
    path = Path(path).resolve()
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return {"path": str(path), "bytes": stat.st_size, "sha256": digest.hexdigest()}


class Job:
    def __init__(self, manager, record):
        self.manager, self.record = manager, record
        self.id, self.params = record["id"], record["params"]
        self.cancel_event = threading.Event()
        self.committed = False
        self.lock = threading.RLock()
        self.work_dir = manager.app.data_dir / "jobs" / self.id
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def save(self):
        self.record["updated_at"] = now()
        self.manager.storage.put("jobs", self.id, self.record)
        self.manager.app.emit("job.updated", job=copy.deepcopy(self.record))

    def progress(self, percent, message):
        self.check_cancelled()
        with self.lock:
            self.record.update(progress=max(0, min(100, float(percent))), message=str(message))
            self.save()

    def check_cancelled(self):
        if self.cancel_event.is_set() and not self.committed:
            raise Cancelled("任务已取消")

    def mark_committed(self):
        # Atomic publication/replacement has finished. A late cancel cannot undo
        # it and must not make a successful data change look unperformed.
        self.committed = True

    def artifact(self, path, kind="file", label=None):
        path = Path(path).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"输出文件不存在或为空：{path.name}")
        try:
            relative = str(path.relative_to(self.manager.app.data_dir)).replace("\\", "/")
        except ValueError:
            relative = None
        item = {"path": str(path), "relative_path": relative, "kind": kind, "label": label or path.name, "size": path.stat().st_size}
        with self.lock:
            self.record["artifacts"] = [a for a in self.record["artifacts"] if a["path"] != str(path)] + [item]
            self.save()
        return item

    def checkpoint(self, step, inputs, parameters, run):
        """Reuse only validated complete artifacts with matching input and parameters."""
        key = fingerprint({"step": step, "inputs": inputs, "parameters": parameters, "scope": getattr(self, "checkpoint_scope", None), "schema": 1})
        cached = self.manager.storage.get("checkpoints", key)
        if cached:
            try:
                if all(file_signature(a["path"]) == a for a in cached["files"]):
                    return cached["result"]
            except (OSError, ValueError):
                pass
        self.check_cancelled()
        result, paths = run()
        self.check_cancelled()
        files = [file_signature(p) for p in paths]
        if any(not f["bytes"] for f in files):
            raise ValueError(f"{step} 产生空输出，未写入检查点")
        self.manager.storage.put("checkpoints", key, {"result": result, "files": files})
        return result

    def run_process(self, args, *, cwd=None, env=None, timeout=None):
        """Hide Windows child consoles, retain diagnostics, cancel entire process tree."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log_path = self.work_dir / ("process-" + uuid.uuid4().hex[:8] + ".log")
        started = time.monotonic()
        with log_path.open("wb") as log:
            process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            try:
                while process.poll() is None:
                    self.check_cancelled()
                    if timeout and time.monotonic() - started > timeout:
                        raise TimeoutError("子进程执行超时")
                    self.cancel_event.wait(.15)
            except BaseException:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=flags)
                else:
                    process.kill()
                process.wait(timeout=10)
                raise
        output = log_path.read_text("utf-8", errors="replace")
        if process.returncode:
            raise RuntimeError(f"{Path(str(args[0])).name} 执行失败（{process.returncode}）：{output[-5000:]}")
        return output


class Jobs:
    def __init__(self, app):
        self.app, self.storage = app, app.storage
        self.runners, self.active = {}, {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="toolbox-job")
        self.gpu_lock = threading.Lock()
        self.lock = threading.RLock()
        for record in self.storage.all("jobs"):
            if record["status"] in ("queued", "running", "cancelling"):
                record.update(status="interrupted", message="上次退出时任务未完成，可继续重试。")
                self.storage.put("jobs", record["id"], record)

    def register(self, tool, runner):
        self.runners[tool] = runner

    @contextlib.contextmanager
    def gpu_slot(self, job):
        while not self.gpu_lock.acquire(timeout=.2):
            job.check_cancelled()
        try:
            job.check_cancelled()
            yield
        finally:
            self.gpu_lock.release()

    def submit(self, tool, params, runner=None):
        if tool.startswith("fnconnect.") and any(k in params for k in ("password", "cookie", "secret", "authorization")):
            raise ValueError("飞牛凭据只能通过专用连接接口提交，不得写入任务记录")
        if tool == "shizuku.pair" and any(key in params for key in ("code", "pairing_code", "password")):
            raise ValueError("无线配对请通过 shizuku.pair 专用接口提交，配对码不得写入任务记录")
        if tool in ("webdav.upload", "webdav.restore") and any(
            key in params for key in ("password", "backup_password", "username", "url", "authorization", "remote_path")
        ):
            raise ValueError("WebDAV 同步请通过专用接口提交，连接凭据及备份密码不得写入任务记录")
        if tool in ("backup-export", "backup-import") and "password" in params:
            raise ValueError("加密备份请通过 backups.export / backups.import 接口提交，密码不得写入任务记录")
        runner = runner or self.runners.get(tool)
        if runner is None:
            raise ValueError(f"未知任务工具：{tool}")
        # Register stable tools for retries; closure handlers should also register explicitly.
        self.runners.setdefault(tool, runner)
        record = {"id": uuid.uuid4().hex, "tool": tool, "params": copy.deepcopy(params), "status": "queued",
                  "progress": 0, "message": "等待开始", "created_at": now(), "artifacts": []}
        job = Job(self, record)
        with self.lock:
            self.active[job.id] = job
            job.save()
            result = copy.deepcopy(record)
            self.pool.submit(self._run, job, runner)
        return result

    def _run(self, job, runner):
        try:
            job.check_cancelled()
            job.record.update(status="running", started_at=now())
            job.save()
            result = runner(job)
            job.check_cancelled()
            job.record.update(status="completed", progress=100, message="已完成", result=result)
        except Cancelled:
            job.record.update(status="cancelled", message="已取消")
        except Exception as exc:
            import logging
            logging.exception("Job %s failed", job.id)
            job.record.update(status="failed", message=str(exc), error=str(exc))
        finally:
            job.record["finished_at"] = now()
            job.save()
            with self.lock:
                self.active.pop(job.id, None)

    def list(self):
        return sorted(self.storage.all("jobs"), key=lambda j: j["created_at"], reverse=True)

    def get(self, id):
        record = self.storage.get("jobs", id)
        if not record:
            raise ValueError("任务不存在")
        return record

    def cancel(self, id):
        with self.lock:
            job = self.active.get(id)
            if job and not job.committed:
                job.cancel_event.set()
                job.record.update(status="cancelling", message="正在停止…")
                job.save()
        return self.get(id)

    def retry(self, id):
        record = self.get(id)
        if record["status"] in ("queued", "running", "cancelling"):
            raise ValueError("运行中的任务不能重试")
        return self.submit(record["tool"], record["params"])

    def close(self):
        with self.lock:
            for job in self.active.values():
                job.cancel_event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
