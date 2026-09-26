import importlib.util
import os
import platform
import shutil
import threading
from pathlib import Path

from . import __version__
from .jobs import Jobs
from .models import Models
from .providers import Providers
from .settings import Settings
from .setup import ModelSetup
from .storage import Storage
from . import subtitles


class App:
    def __init__(self, data_dir, emit=None, *, register_features=True, register_live=True):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.handlers = {}
        self._emit = emit or (lambda event: None)
        self.maintenance = False
        self.data_lock = threading.RLock()
        self.storage = Storage(self.data_dir)
        self.settings = Settings(self.data_dir)
        self.jobs = Jobs(self)
        self.models = Models(self)
        from .local_llm import LocalLLM
        self.local_llm = LocalLLM(self)
        self.providers = Providers(self.settings, local_llm=self.local_llm, usage=self.storage)
        self.model_setup = ModelSetup(self)
        self.register("app.info", lambda p: {"name":"WinToolbox","version":__version__,"data_dir":str(self.data_dir),"ffmpeg":os.getenv("WINTOOLBOX_FFMPEG") or shutil.which("ffmpeg"),"platform":platform.platform()})
        self.register("app.prepare_exit", lambda p: self.prepare_exit())
        self.register("settings.get", lambda p: self.settings.get())
        self.register("settings.update", lambda p: self.settings.update(p.get("settings", p)))
        self.register("setup.get", lambda p: self.model_setup.get())
        self.register("setup.apply", self.model_setup.apply)
        self.register("setup.install", self.model_setup.install)
        self.jobs.register("setup.install", self.model_setup.install_job)
        self.register("providers.test", lambda p: self.providers.test(p["provider_id"]))
        self.register("providers.models", lambda p: self.providers.models(p["provider_id"]))
        self.register("providers.usage", lambda p: self.storage.usage_summary(p["provider_id"]))
        from .voices import catalogue
        self.register("practice.voices", lambda p: catalogue(self.settings))
        self.register("models.list", lambda p: self.models.list())
        self.register("models.install", lambda p: self.jobs.submit("models.install", p))
        self.register("models.remove", lambda p: self.models.remove(p["model_id"]))
        self.jobs.register("models.install", self.models.install)
        self.jobs.register("models.export", self.models.export_package)
        self.jobs.register("models.import", self.models.import_package)
        self.register("models.export", lambda p: self.jobs.submit("models.export", p))
        self.register("models.import", lambda p: self.jobs.submit("models.import", p))
        self.register("jobs.list", lambda p: {"jobs": self.jobs.list()})
        self.register("jobs.get", lambda p: self.jobs.get(p["id"]))
        self.register("jobs.submit", lambda p: self.jobs.submit(p["tool"], p.get("params", {})))
        self.register("jobs.cancel", lambda p: self.jobs.cancel(p["id"]))
        self.register("jobs.retry", lambda p: self.jobs.retry(p["id"]))
        self.register("subtitles.load", lambda p: subtitles.load(p["path"]))
        self.register("subtitles.save", lambda p: {"path": subtitles.save(p["path"], p["segments"], p.get("bilingual", True))})
        from .media import register
        register(self)
        if register_features:
            from .features import register_all
            register_all(self)
        if register_live and importlib.util.find_spec("toolbox.live"):
            from .live import register as register_live_handlers
            register_live_handlers(self)
        from .document_runtime import register as register_document_runtime
        register_document_runtime(self)
        if hasattr(self, "ledger_auto_sync"):
            self.ledger_auto_sync()

    def register(self, name, handler):
        if name in self.handlers:
            raise ValueError(f"重复 RPC 注册：{name}")
        self.handlers[name] = handler

    def call(self, method, params=None):
        if method not in self.handlers:
            raise LookupError(f"方法不存在：{method}")
        if params is not None and not isinstance(params, dict):
            raise ValueError("params 必须为 JSON 对象")
        # Snapshot/restore takes this gate before jobs/storage locks. Synchronous
        # edits already in flight finish first; new edits cannot race replacement.
        if method in ("app.info", "jobs.get", "jobs.list", "jobs.cancel"):
            return self.handlers[method](params or {})
        if self.maintenance:
            raise RuntimeError("正在同步或恢复数据，请稍后再操作")
        with self.data_lock:
            if self.maintenance:
                raise RuntimeError("正在同步或恢复数据，请稍后再操作")
            return self.handlers[method](params or {})

    def emit(self, type, **fields):
        self._emit({"type": type, **fields})

    def after_restore(self):
        if hasattr(self, "filesync"):
            self.filesync.init_tables()
            self.filesync.due.clear()
            self.filesync.pending.clear()
            # Restored rules may refer to a different device. Require deliberate re-enabling.
            for rule in self.filesync.rules():
                rule['auto'] = False
                self.filesync.put(rule)
        if hasattr(self, "memory_after_restore"):
            self.memory_after_restore()
        if hasattr(self, "ledger_after_restore"):
            self.ledger_after_restore()
        self.settings.reload()
        self.reset_local_llm()

    def reset_local_llm(self):
        from .local_llm import LocalLLM
        self.local_llm.close()
        self.local_llm = LocalLLM(self)
        self.providers.local_llm = self.local_llm

    def before_restore(self, current_job_id):
        with self.jobs.lock:
            if any(id != current_job_id for id in self.jobs.active):
                raise RuntimeError("请等待其他任务结束或取消后再恢复备份")
        live = getattr(self, "live", None)
        if live is not None and (getattr(live, "running", False) or getattr(live, "session", None)):
            raise RuntimeError("请先停止实时会话后再恢复备份")
        holder = getattr(self, "live_holder", None)
        if holder and holder.get("session") and not holder["session"].stop_event.is_set():
            raise RuntimeError("请先停止实时会话后再恢复备份")
        self.local_llm.close()
        if hasattr(self, "memory_before_restore"):
            self.memory_before_restore()
        if hasattr(self, "ledger_before_restore"):
            self.ledger_before_restore()

    def close(self):
        if hasattr(self, "ledger_close"):
            self.ledger_close()
        if hasattr(self, "memory_cleaner_close"):
            self.memory_cleaner_close()
        if hasattr(self, "filesync"):
            self.filesync.close()
        if hasattr(self, "memory_stop"):
            self.memory_stop()
        if hasattr(self, "gpu_guard_close"):
            self.gpu_guard_close()
        if hasattr(self, "fnconnect_close"):
            self.fnconnect_close()
        holder = getattr(self, "live_holder", None)
        if holder and holder.get("session") and not holder["session"].stop_event.is_set():
            holder["session"].stop()
        if hasattr(self, "live") and hasattr(self.live, "stop"):
            try:
                self.live.stop({})
            except TypeError:
                self.live.stop()
        self.jobs.close()
        if hasattr(self, "memory_close"):
            self.memory_close()
        self.local_llm.close()
        self.providers.client.close()

    def prepare_exit(self):
        if hasattr(self, "ledger_close"):
            self.ledger_close()
        if hasattr(self, "filesync"):
            self.filesync.close()
        if hasattr(self, "gpu_guard_close"):
            self.gpu_guard_close()
        if hasattr(self, "fnconnect_close"):
            self.fnconnect_close()
        holder = getattr(self, "live_holder", None)
        if holder and holder.get("session") and not holder["session"].stop_event.is_set():
            holder["session"].stop()
        for job in list(self.jobs.active.values()):
            job.cancel_event.set()
        self.local_llm.close()
        return {"ok": True}
