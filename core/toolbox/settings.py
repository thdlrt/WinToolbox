import base64
import copy
import ctypes
import json
import os
import threading
from pathlib import Path

BAILIAN_MODELS = {
    "transcribe": "qwen-audio-3.0-asr-flash", "live_asr": "qwen-audio-3.0-asr-flash-streaming",
    "translate": "qwen3.8-flash", "chat": "qwen3.8-flash", "vision": "qwen3.8-flash",
    "embedding": "text-embedding-v4", "tts": "qwen3-tts-flash",
}

DEFAULTS = {
    "providers": [{"id": "dashscope", "name": "百炼", "kind": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "region": "cn"}],
    "roles": {role: {"provider_id": "dashscope", "model": model} for role, model in BAILIAN_MODELS.items()},
    "preferences": {"theme": "system", "record_audio": True}, "presets": [],
}


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def protect(value, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("API 密钥存储需要 Windows DPAPI；此平台只支持无密钥功能及测试注入。")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode("utf-8")
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError()
    try:
        result = ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
    return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")


class Settings:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "settings.json"
        self.secret_path = Path(data_dir) / "secrets.json"
        self.lock = threading.RLock()
        self.reload()

    def reload(self):
        with self.lock:
            self.value = copy.deepcopy(DEFAULTS)
            if self.path.exists():
                self._merge(self.value, json.loads(self.path.read_text("utf-8")))
            self.secrets = json.loads(self.secret_path.read_text("utf-8")) if self.secret_path.exists() else {}

    @staticmethod
    def _merge(target, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                Settings._merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    def get(self):
        with self.lock:
            result = copy.deepcopy(self.value)
            for provider in result["providers"]:
                provider.pop("api_key", None)
                provider["has_key"] = bool(self.secrets.get(provider["id"]))
            return result

    def update(self, patch):
        with self.lock:
            patch = copy.deepcopy(patch)
            new_secrets = copy.deepcopy(self.secrets)
            if "providers" in patch:
                ids = set()
                for provider in patch["providers"]:
                    pid = str(provider.get("id", "")).strip()
                    if not pid or pid in ids:
                        raise ValueError("供应商 ID 不能为空或重复")
                    ids.add(pid)
                    if provider.get("kind") not in ("openai", "dashscope", "gemini"):
                        raise ValueError("不支持的供应商类型")
                    key = provider.pop("api_key", None)
                    provider.pop("has_key", None)
                    if key is not None and key.strip():
                        key = key.strip()
                        if "*" in key or "•" in key or key.lower() in ("masked", "your-api-key", "sk-..."):
                            raise ValueError("请填写真实 API 密钥；掩码不能覆盖已保存密钥")
                        new_secrets[pid] = protect(key)
                    if provider.pop("clear_key", False):
                        new_secrets.pop(pid, None)
                new_secrets = {pid: value for pid, value in new_secrets.items() if pid in ids}
            candidate = copy.deepcopy(self.value)
            self._merge(candidate, patch)
            atomic_json(self.secret_path, new_secrets)
            atomic_json(self.path, candidate)
            self.value, self.secrets = candidate, new_secrets
            return self.get()

    def secret(self, provider_id):
        with self.lock:
            value = self.secrets.get(provider_id)
            return protect(value, decrypt=True) if value else ""

    def export_secrets(self):
        with self.lock:
            return {pid: protect(value, decrypt=True) for pid, value in self.secrets.items()}

    def import_secrets(self, values):
        with self.lock:
            self.secrets = {pid: protect(value) for pid, value in values.items()}
            atomic_json(self.secret_path, self.secrets)
