"""Script tools: one page that lists the local scripts you keep running, runs them
as background jobs with captured output, and never needs to know what they do.

Entries are machine-bound (they point at absolute paths on this computer), so the
list lives in LOCALAPPDATA next to the guard's own state, not in synced app data.
A shipped script is seeded into the scripts directory on first use; once you edit
that copy the toolbox stops overwriting it and only reports that an update exists.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ._common import atomic_write, digest, write_json

ASSETS = Path(__file__).resolve().parent / "script_assets"
BUILTINS = (
    {
        "asset": "dsh-mcp-sync.py",
        "name": "同步 MCP 到 DSH",
        "note": "读取 Codex 配置里的 mcp_servers，写成 DeepSeek Harness 的 dsh-mcp-client 插件行；改动只发生在标记段内。",
    },
    {
        "asset": "dsh-skill-sync.py",
        "name": "同步技能库到 DSH",
        "note": "让 DeepSeek Harness 直接读取 cc-switch 维护的技能库，并点名 Harness 会跳过的技能。",
    },
)
# Extension -> how the runner invokes it. Keeping this list closed means an
# unsupported file fails with a reason instead of a mystery process.
RUNNERS = {".py": "python", ".ps1": "powershell", ".cmd": "cmd", ".bat": "cmd", ".exe": "exe", ".js": "node", ".mjs": "node"}
# Opening these with their shell default would execute them, so the page only
# offers 打开所在文件夹 for them.
EXECUTABLE = (".exe", ".cmd", ".bat", ".com", ".msi")
SCHEMA = 1
MAX_CONSOLE_BYTES = 64 * 1024
RUN_TIMEOUT_SECONDS = 1800
TOOL = "script-tools.run"


def now():
    return datetime.now(timezone.utc).isoformat()


def state_root(params=None):
    """LOCALAPPDATA state, with an explicit override for tests and relocation."""
    explicit = (params or {}).get("state_root")
    if explicit:
        return Path(explicit).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "WinToolbox" / "script-tools"
    return Path.home() / ".wintoolbox" / "script-tools"


def load(root):
    path = Path(root) / "state.json"
    if not path.is_file():
        return {"schema": SCHEMA, "entries": [], "seeded": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(f"脚本清单无法读取：{path}（{exc}）。删除该文件可重新初始化，脚本文件不受影响。") from exc
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise ValueError(f"脚本清单格式不受支持：{path}。删除该文件可重新初始化。")
    value["schema"] = SCHEMA
    if not isinstance(value.get("seeded"), list):
        value["seeded"] = []
    return value


def save(root, state):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "state.json", state)


def seed(state, root, assets=None):
    """Copy shipped scripts once; refresh only copies the user has not edited."""
    root = Path(root)
    assets = Path(assets or ASSETS)
    scripts = root / "scripts"
    for builtin in BUILTINS:
        source = assets / builtin["asset"]
        if not source.is_file():
            continue
        current = digest(source.read_bytes())
        entry = next((item for item in state["entries"] if item.get("builtin") == builtin["asset"]), None)
        if entry is None:
            if builtin["asset"] in state["seeded"]:
                continue  # removed on purpose: do not resurrect it
            scripts.mkdir(parents=True, exist_ok=True)
            target = scripts / builtin["asset"]
            atomic_write(target, source.read_bytes())
            state["entries"].append({
                "id": uuid.uuid4().hex, "name": builtin["name"], "note": builtin["note"], "path": str(target),
                "builtin": builtin["asset"], "seeded_sha256": current, "created_at": now(),
            })
            state["seeded"].append(builtin["asset"])
            continue
        if entry.get("seeded_sha256") == current:
            entry.pop("update_available", None)
            continue
        target = Path(entry["path"])
        if target.is_file() and digest(target.read_bytes()) == entry.get("seeded_sha256"):
            atomic_write(target, source.read_bytes())
            entry["seeded_sha256"] = current
            entry.pop("update_available", None)
        else:
            entry["update_available"] = True
    return state


def command_for(path):
    """The argv and runner name for one script path."""
    path = Path(path)
    runner = RUNNERS.get(path.suffix.lower())
    if runner is None:
        raise ValueError(f"不支持的文件类型：{path.suffix or path.name}；支持 {'、'.join(sorted(RUNNERS))}")
    if runner == "python":
        return [sys.executable, str(path)], runner
    if runner == "powershell":
        shell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
        if not shell:
            raise ValueError("找不到 PowerShell，无法运行 .ps1 脚本")
        return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)], runner
    if runner == "cmd":
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", str(path)], runner
    if runner == "node":
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            raise ValueError("找不到 node，无法运行 JavaScript 脚本；请改用 .py 或 .ps1")
        return [node, str(path)], runner
    return [str(path)], runner


def view(entry):
    path = Path(entry["path"])
    suffix = path.suffix.lower()
    exists = path.is_file()
    return {
        "id": entry["id"], "name": entry["name"], "note": entry.get("note", ""), "path": str(path),
        "folder": str(path.parent), "runner": RUNNERS.get(suffix, ""), "exists": exists,
        "builtin": bool(entry.get("builtin")), "builtin_asset": entry.get("builtin") or None,
        "update_available": bool(entry.get("update_available")),
        "openable": exists and suffix not in EXECUTABLE, "runnable": exists and suffix in RUNNERS,
        "last_job_id": entry.get("last_job_id"),
    }


def listing(params):
    root = state_root(params)
    state = seed(load(root), root)
    save(root, state)
    return {"entries": [view(entry) for entry in state["entries"]], "scripts_dir": str(root / "scripts")}


def _prepare(params):
    root = state_root(params)
    state = seed(load(root), root)
    return root, state


def _require(state, entry_id):
    entry = next((item for item in state["entries"] if item["id"] == entry_id), None)
    if entry is None:
        raise ValueError("脚本条目不存在，可能已被删除")
    return entry


def _text(params, key, limit):
    return str(params.get(key) or "").strip()[:limit]


def add(params):
    root, state = _prepare(params)
    path = Path(str(params.get("path") or "")).expanduser()
    if not path.is_file():
        raise ValueError(f"找不到脚本文件：{path}")
    if path.suffix.lower() not in RUNNERS:
        raise ValueError(f"不支持的文件类型：{path.suffix or path.name}；支持 {'、'.join(sorted(RUNNERS))}")
    resolved = path.resolve()
    if any(Path(entry["path"]).resolve() == resolved for entry in state["entries"]):
        raise ValueError("该脚本已在列表中")
    entry = {
        "id": uuid.uuid4().hex, "name": _text(params, "name", 80) or resolved.stem,
        "note": _text(params, "note", 400), "path": str(resolved), "created_at": now(),
    }
    state["entries"].append(entry)
    save(root, state)
    return {"entries": [view(item) for item in state["entries"]], "id": entry["id"]}


def update(params):
    root, state = _prepare(params)
    entry = _require(state, params.get("id"))
    if "name" in params:
        entry["name"] = _text(params, "name", 80) or Path(entry["path"]).stem
    if "note" in params:
        entry["note"] = _text(params, "note", 400)
    save(root, state)
    return {"entries": [view(item) for item in state["entries"]], "id": entry["id"]}


def remove(params):
    root, state = _prepare(params)
    entry = _require(state, params.get("id"))
    state["entries"] = [item for item in state["entries"] if item["id"] != entry["id"]]
    save(root, state)
    return {"entries": [view(item) for item in state["entries"]], "path": entry["path"]}


def reset(params):
    """Put a shipped script back to the version this build carries."""
    root, state = _prepare(params)
    entry = _require(state, params.get("id"))
    asset = entry.get("builtin")
    source = Path(ASSETS) / str(asset) if asset else None
    if source is None or not source.is_file():
        raise ValueError("只有内置脚本可以恢复内置版本")
    atomic_write(Path(entry["path"]), source.read_bytes())
    entry["seeded_sha256"] = digest(source.read_bytes())
    entry.pop("update_available", None)
    save(root, state)
    return {"entries": [view(item) for item in state["entries"]], "id": entry["id"]}


def run(app, params):
    root, state = _prepare(params)
    entry = _require(state, params.get("id"))
    path = Path(entry["path"])
    if not path.is_file():
        raise ValueError(f"找不到脚本文件：{path}")
    command_for(path)  # fail here, with a reason, instead of inside the job
    record = app.jobs.submit(TOOL, {"entry_id": entry["id"], "name": entry["name"], "path": str(path)})
    entry["last_job_id"] = record["id"]
    save(root, state)
    return record


def console(app, params):
    """The job record plus the tail of its console log, for a live terminal."""
    record = app.jobs.get(str(params.get("job_id") or ""))
    path = Path(str(record.get("params", {}).get("path") or ""))
    log = Path(app.data_dir) / "jobs" / record["id"] / "console.log"
    output = ""
    try:
        size = log.stat().st_size
        with log.open("rb") as handle:
            handle.seek(max(0, size - MAX_CONSOLE_BYTES))
            output = handle.read(MAX_CONSOLE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        output = ""
    return {
        "job_id": record["id"], "status": record["status"], "progress": record.get("progress", 0),
        "message": record.get("message", ""), "error": record.get("error"), "created_at": record["created_at"],
        "finished_at": record.get("finished_at"), "name": record.get("params", {}).get("name", ""),
        "path": str(path), "runner": RUNNERS.get(path.suffix.lower(), ""), "output": output.strip("\r\n"),
    }


def register(app):
    def runner(job):
        path = Path(str(job.params.get("path") or ""))
        if not path.is_file():
            raise ValueError(f"找不到脚本文件：{path}")
        argv, runner_name = command_for(path)
        job.progress(1, f"运行 {path.name}")
        output = job.run_process(argv, cwd=str(path.parent), timeout=RUN_TIMEOUT_SECONDS, log_name="console.log")
        return {"path": str(path), "runner": runner_name, "bytes": len(output.encode("utf-8", "replace"))}

    app.jobs.register(TOOL, runner)
    app.register("script_tools.list", lambda p: listing(p))
    app.register("script_tools.add", lambda p: add(p))
    app.register("script_tools.update", lambda p: update(p))
    app.register("script_tools.remove", lambda p: remove(p))
    app.register("script_tools.reset", lambda p: reset(p))
    app.register("script_tools.run", lambda p: run(app, p))
    app.register("script_tools.console", lambda p: console(app, p))
