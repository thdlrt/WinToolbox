"""Read the model catalogue and narrowly edit top-level Codex settings."""
import json
import os
import re
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path

import tomlkit

from ._common import atomic_write, digest

_LOCK = threading.RLock()


def paths(params):
    home = Path(params.get("home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    config = Path(params.get("config_path") or home / "config.toml").expanduser().resolve()
    cache = Path(params.get("cache_path") or home / "models_cache.json").expanduser().resolve()
    if config.suffix.lower() != ".toml" or cache.name.lower() == "auth.json":
        raise ValueError("请选择 config.toml 和模型缓存，不能读取认证文件")
    return config, cache


def read_config(path):
    raw = path.read_bytes() if path.exists() else b""
    text = raw.decode("utf-8-sig")
    document = tomlkit.parse(text)
    return raw, text, document, digest((b"exists:" if path.exists() else b"missing:") + raw)


def scan(params):
    config, cache = paths(params)
    if not cache.is_file():
        raise ValueError(f"找不到模型缓存：{cache}。请先启动 Codex 更新模型目录。")
    try:
        payload = json.loads(cache.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("模型缓存无法解析，请在 Codex 中刷新后重试") from exc
    rows = payload if isinstance(payload, list) else payload.get("models", payload.get("data", []))
    if not isinstance(rows, list):
        raise ValueError("模型缓存格式不受支持")
    models = []
    for item in rows:
        if not isinstance(item, dict) or not (item.get("slug") or item.get("id")):
            continue
        if item.get("visibility") == "hide" and not params.get("include_hidden"):
            continue
        model_id = item.get("slug") or item["id"]
        def number(key):
            value = item.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        models.append({"id": model_id, "name": item.get("display_name", model_id),
                       "default_context": number("context_window"), "max_context": number("max_context_window"),
                       "context_window": number("context_window"), "max_context_window": number("max_context_window"),
                       "effective_percent": number("effective_context_window_percent"), "visibility": item.get("visibility", "list")})
    if not models:
        raise ValueError("模型缓存中没有可用模型")
    _, _, document, current_hash = read_config(config)
    return {"models": sorted(models, key=lambda x: x["name"]), "current": {"model": document.get("model"), "context": document.get("model_context_window")},
            "config_path": str(config), "cache_path": str(cache), "hash": current_hash,
            "notice": "修改用户级配置。现有会话或项目配置可能覆盖这些值；重新启动任务后检查实际模型。"}


def preview(params):
    result = scan(params)
    selected = next((m for m in result["models"] if m["id"] == params.get("model")), None)
    if selected is None:
        raise ValueError("请选择缓存中的准确模型 ID")
    context = str(params.get("context", "default")).strip().lower()
    if context in ("default", "max"):
        tokens = selected[f"{context}_context"]
        if tokens is None:
            raise ValueError("模型缓存未提供该上下文大小，请输入自定义值")
    else:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", context)
        if not match:
            raise ValueError("上下文应为正整数、272k、1m、default 或 max")
        value = Decimal(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2]]
        if value != value.to_integral_value() or not 0 < value <= 2**63 - 1:
            raise ValueError("上下文必须是有效的正整数")
        tokens = int(value)
    if selected["max_context"] and tokens > selected["max_context"]:
        raise ValueError(f"上下文超过缓存中的最大值 {selected['max_context']}")
    config = Path(result["config_path"])
    raw, before, document, current_hash = read_config(config)
    document["model"] = selected["id"]
    document["model_context_window"] = tokens
    after = tomlkit.dumps(document)
    if "\r\n" in before:
        after = after.replace("\r\n", "\n").replace("\n", "\r\n")
    return {"before": before, "after": after, "hash": current_hash, "model": selected["id"], "context": tokens,
            "config_path": str(config), "bom": raw.startswith(b"\xef\xbb\xbf"), "notice": result["notice"]}


def replace_config(config, content, expected_hash):
    if not isinstance(expected_hash, str) or not expected_hash:
        raise ValueError("请先预览，并传入预览版本 hash")
    raw, _, _, actual_hash = read_config(config)
    if actual_hash != expected_hash:
        raise ValueError("配置已被外部修改，请重新扫描并预览")
    backup = config.with_name(f"{config.name}.{time.strftime('%Y%m%d-%H%M%S')}.{uuid.uuid4().hex[:8]}.bak")
    if config.exists():
        atomic_write(backup, raw)
    else:
        backup = None
    if read_config(config)[3] != expected_hash:
        raise ValueError("写入前配置发生变化，请重新预览")
    atomic_write(config, content)
    return {"ok": True, "backup_path": str(backup) if backup else None, "config_path": str(config), "hash": read_config(config)[3]}


def apply(params):
    with _LOCK:
        change = preview(params)
        data = (b"\xef\xbb\xbf" if change["bom"] else b"") + change["after"].encode("utf-8")
        return replace_config(Path(change["config_path"]), data, params.get("expected_hash"))


def backups(params):
    config, _ = paths(params)
    return {"backups": [{"path": str(p), "created_at": p.stat().st_mtime, "size": p.stat().st_size}
                        for p in sorted(config.parent.glob(config.name + ".*.bak"), reverse=True) if p.is_file()]}


def restore(params):
    with _LOCK:
        config, _ = paths(params)
        backup = Path(params["backup_path"]).resolve()
        if backup.parent != config.parent or not backup.name.startswith(config.name + ".") or backup.suffix != ".bak":
            raise ValueError("只能恢复所选配置旁的工具备份文件")
        data = backup.read_bytes()
        tomlkit.parse(data.decode("utf-8-sig"))
        return replace_config(config, data, params.get("expected_hash"))


def register(app):
    for name, handler in (("scan", scan), ("preview", preview), ("apply", apply), ("backups", backups), ("restore", restore)):
        app.register("codex." + name, handler)
