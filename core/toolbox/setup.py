"""Small, explicit model setup profiles shared by all processing tools."""
import copy
import ctypes
import os
import platform
import shutil
import subprocess

from .settings import BAILIAN_MODELS


EMBEDDING_MODEL = "embeddinggemma:latest"
PRESETS = [
    {"id": "light", "name": "轻量", "description": "低占用，适合短音频和简单问答", "hardware_hint": "建议 8 GB 以上内存；无需独显，CPU 转写", "asr_engine": "faster-whisper", "asr_model": "faster-whisper-small", "asr_device": "cpu", "asr_compute_type": "int8", "llm_model": "qwen3.5:0.8b", "model_ids": ["faster-whisper-small", "local-qwen35-08b", "local-embedding"]},
    {"id": "balanced", "name": "均衡", "description": "日常转写、翻译和资料问答", "hardware_hint": "建议 16 GB 以上内存、8 GB 以上 NVIDIA 显存；无独显会较慢", "asr_engine": "faster-whisper", "asr_model": "faster-whisper-turbo", "asr_device": "auto", "asr_compute_type": "default", "llm_model": "qwen3.5:4b", "model_ids": ["faster-whisper-turbo", "local-qwen35-4b", "local-embedding"]},
    {"id": "quality", "name": "质量", "description": "复杂语音和问答，资源占用较高", "hardware_hint": "建议 32 GB 以上内存、16 GB 以上 NVIDIA 显存", "asr_engine": "faster-whisper", "asr_model": "faster-whisper-large-v3", "asr_device": "auto", "asr_compute_type": "default", "llm_model": "qwen3.5:9b", "model_ids": ["faster-whisper-large-v3", "local-qwen35-9b", "local-embedding"]},
]


def preset_by_id(value):
    preset = next((item for item in PRESETS if item["id"] == value), None)
    if not preset:
        raise ValueError("请选择轻量、均衡或质量预设")
    return copy.deepcopy(preset)


def detect_hardware():
    """Best effort; never install a driver or expose a helper window."""
    result = {"cpu": platform.processor() or platform.machine(), "ram_gb": None, "gpus": [], "vram_gb": None, "note": "硬件建议为估计；速度取决于音频、模型及同时运行的程序。"}
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
            value = MemoryStatus()
            value.length = ctypes.sizeof(value)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
                result["ram_gb"] = round(value.total / 1024**3, 1)
        elif hasattr(os, "sysconf"):
            result["ram_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    except (AttributeError, OSError, ValueError):
        pass
    binary = shutil.which("nvidia-smi")
    if not binary and os.name == "nt":
        candidate = os.path.join(os.getenv("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        if os.path.isfile(candidate):
            binary = candidate
    if binary:
        try:
            probe = subprocess.run([binary, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in probe.stdout.splitlines() if probe.returncode == 0 else []:
                name, _, memory = line.rpartition(",")
                if name and memory.strip().isdigit():
                    result["gpus"].append({"name": name.strip(), "vram_gb": round(int(memory) / 1024, 1)})
            if result["gpus"]:
                result["vram_gb"] = max(gpu["vram_gb"] for gpu in result["gpus"])
        except (OSError, subprocess.SubprocessError):
            pass
    return result


def recommended_preset(hardware):
    ram, vram = hardware.get("ram_gb") or 0, hardware.get("vram_gb") or 0
    if ram >= 31 and vram >= 15:
        return "quality"
    if ram >= 15 and vram >= 7.5:
        return "balanced"
    return "light"


def bailian_provider(values):
    providers = [item for item in values.get("providers", []) if item.get("kind") == "dashscope"]
    keyed = [item for item in providers if item.get("has_key")]
    default = next((item for item in keyed if item["id"] == "dashscope"), None)
    if default:
        return default
    role_ids = {item.get("provider_id") for item in values.get("roles", {}).values()}
    selected = next((item for item in keyed if item["id"] in role_ids), None)
    if selected:
        return selected
    if keyed:
        return keyed[0]
    return next((item for item in providers if item["id"] == "dashscope"), providers[0] if providers else {})


class ModelSetup:
    def __init__(self, app):
        self.app = app
        self._hardware = None

    def ensure_idle(self):
        if self.app.jobs.active:
            raise RuntimeError("请等待任务结束或取消任务后再切换模型配置")
        live = getattr(self.app, "live", None)
        if live is not None and (getattr(live, "running", False) or getattr(live, "session", None)):
            raise RuntimeError("请先停止实时会话后再切换模型配置")
        holder = getattr(self.app, "live_holder", None)
        if holder and holder.get("session") and not holder["session"].stop_event.is_set():
            raise RuntimeError("请先停止实时会话后再切换模型配置")

    def get(self):
        values = self.app.settings.get()
        prefs = values.get("preferences", {})
        provider = bailian_provider(values)
        if self._hardware is None:
            self._hardware = detect_hardware()
        models = {item["id"]: item for item in self.app.models.list()["models"]}
        presets = []
        for item in PRESETS:
            status = [models[model_id] for model_id in item["model_ids"]]
            presets.append({**copy.deepcopy(item), "installed": all(model["installed"] for model in status), "installed_count": sum(model["installed"] for model in status), "model_count": len(status), "embedding_model": EMBEDDING_MODEL})
        return {"mode": prefs.get("model_mode", "bailian"), "preset": prefs.get("local_preset", recommended_preset(self._hardware)), "region": "intl" if provider.get("region") in ("intl", "sg") else "cn", "provider_id": provider.get("id", "dashscope"), "has_key": provider.get("has_key", False), "configured": bool(prefs.get("model_mode")), "presets": presets, "hardware": copy.deepcopy(self._hardware), "recommended_preset": recommended_preset(self._hardware)}

    def apply(self, params):
        mode = params.get("mode")
        if mode not in ("bailian", "local"):
            raise ValueError("请选择阿里云百炼 API 或本地模式")
        with self.app.jobs.lock, self.app.settings.lock:
            self.ensure_idle()
            current = self.app.settings.get()
            prefs = {"model_mode": mode}
            patch = {"preferences": prefs}
            if mode == "local":
                preset = preset_by_id(params.get("preset", current.get("preferences", {}).get("local_preset", "light")))
                prefs.update({"local_preset": preset["id"], **{key: preset[key] for key in ("asr_engine", "asr_model", "asr_device", "asr_compute_type")}})
                patch["roles"] = {role: {"provider_id": "local", "model": preset["llm_model"]} for role in ("chat", "translate", "vision")}
                patch["roles"].update({"embedding": {"provider_id": "local", "model": EMBEDDING_MODEL}, "transcribe": {"provider_id": "local", "model": preset["asr_model"]}, "live_asr": {"provider_id": "local", "model": preset["asr_model"]}, "tts": {"provider_id": "local", "model": "cosyvoice"}})
            else:
                providers = current["providers"]
                provider = bailian_provider(current)
                if not provider:
                    existing_ids = {item["id"] for item in providers}
                    provider_id = "dashscope"
                    while provider_id in existing_ids:
                        provider_id += "-bailian"
                    provider = {"id": provider_id}
                    providers.append(provider)
                region = params.get("region", "intl" if provider.get("region") in ("intl", "sg") else "cn")
                if region not in ("cn", "intl"):
                    raise ValueError("百炼地域请选择北京或新加坡")
                api_key = str(params.get("api_key") or "").strip()
                if not api_key and not provider.get("has_key"):
                    raise ValueError("请填写阿里云百炼 API Key")
                provider.update({"name": "阿里云百炼", "kind": "dashscope", "region": region, "base_url": "https://dashscope" + ("-intl" if region == "intl" else "") + ".aliyuncs.com/compatible-mode/v1"})
                # Region changes must not retain an advanced, old native endpoint.
                provider.pop("native_url", None)
                if api_key:
                    provider["api_key"] = api_key
                patch["providers"] = providers
                patch["roles"] = {role: {"provider_id": provider["id"], "model": model} for role, model in BAILIAN_MODELS.items()}
                prefs.update({"asr_engine": "api", "asr_model": "", "asr_device": "auto", "asr_compute_type": "default"})
            result = self.app.settings.update(patch)
            if mode == "bailian":
                self.app.reset_local_llm()
            return result

    def install(self, params):
        preset = preset_by_id(params.get("preset"))
        return self.app.jobs.submit("setup.install", {"preset": preset["id"]})

    def install_job(self, job):
        preset = preset_by_id(job.params["preset"])
        installed = []
        for index, model_id in enumerate(preset["model_ids"]):
            job.check_cancelled()
            status = next(item for item in self.app.models.list()["models"] if item["id"] == model_id)
            if not status["installed"]:
                parent = job
                class InstallStep:
                    params = {"model_id": model_id, "cpu_only": preset["id"] == "light"}
                    def progress(self, percent, message):
                        parent.progress((index + percent / 100) / len(preset["model_ids"]) * 100, message)
                    def __getattr__(self, name):
                        return getattr(parent, name)
                self.app.models.install(InstallStep())
            installed.append(model_id)
            job.progress((index + 1) / len(preset["model_ids"]) * 100, f"已准备 {status['name']}")
        return {"preset": preset["id"], "model_ids": installed, "installed": True}
