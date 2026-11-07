"""硬盘挂载：把 SFTP / WebDAV 挂成 Windows 盘符。

行为对标本机 RaiDrive 2021.12.35（见 docs/NETMOUNT.md 的逆向依据）：
远端文件系统经用户态驱动以盘符呈现，SFTP/WebDAV 账号密码按原样使用，
盘符 + 只读 + 自动重连 + 自动挂载都是每条挂载的选项。差异只有引擎：
这里用随包的 rclone + 用户安装的 WinFsp，而不是 RaiDrive 的 CBFS 驱动。

凭据只存当前用户 DPAPI 密文；rclone 子进程用环境变量接收配置，密码先经
`rclone obscure`（stdin 传入）遮蔽，不进命令行、不进任务参数。挂载随工具箱
退出卸载；auto_mount 的条目在工具箱启动后自动重新挂载。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from toolbox.settings import protect

from ._common import atomic_write

SCHEMA = 1
KINDS = ("sftp", "webdav")
CACHE_MODES = ("off", "minimal", "writes", "full")
DEFAULT_PORTS = {"sftp": 22, "webdav": 80}
WINFSP_URL = "https://winfsp.dev/rel/"
MAX_DRIVES = 24
READY_TIMEOUT_SECONDS = 30
RESTART_GRACE_SECONDS = 10
RESTART_DELAY_SECONDS = 5
RESTART_LIMIT = 10
RESTART_RESET_SECONDS = 300
CREDENTIAL_TTL_SECONDS = 300
LOG_TAIL_BYTES = 8 * 1024
TOOL = "netmount.mount"
TEST_TOOL = "netmount.test"

RAIDRIVE_LOG_DIRECTORY = Path(os.getenv("ProgramData", r"C:\ProgramData")) / "OpenBoxLab" / "RaiDrive" / "log"
ENTRY_RE = re.compile(r"\] (SFTP|WebDAV) \(([^)]+)\) : Drive Created")
AUTH_RE = re.compile(r"\] (?:SFTP|WebDAV) API : (.+?) : AUTH CONNECT : ADDRESS : (\S+) :")
INIT_RE = re.compile(r"\] FILESYSTEM : (.+?) : INIT DONE : DriveLetter : ([A-Z]):, IsNetwork : \w+, IsGlobal : \w+, IsReadOnly : (\w+)")


def engine_path():
    root = Path(os.getenv("WINTOOLBOX_TOOLS", str(Path(__file__).resolve().parents[3] / ".build")))
    return root / "netmount" / "rclone.exe"


def engine_available():
    return os.name == "nt" and engine_path().is_file()


def engine_version(path=None):
    path = Path(path or engine_path())
    if not path.is_file():
        return None
    try:
        output = subprocess.run([str(path), "version"], capture_output=True, text=True,
                                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"rclone (v[\w.]+)", output.stdout or "")
    return match.group(1) if match else None


def winfsp_installed():
    if os.name != "nt":
        return False
    try:
        import winreg
    except ImportError:
        return False
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY, 0):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WinFsp", 0, winreg.KEY_READ | view):
                return True
        except OSError:
            continue
    return False


def taken_letters():
    if os.name != "nt":
        return set()
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return {chr(ord("A") + bit) for bit in range(26) if mask >> bit & 1}


def log_dir(data_dir):
    return Path(data_dir) / "netmount" / "logs"


def config_path(data_dir):
    return Path(data_dir) / "netmount.json"


def load_config(data_dir):
    path = config_path(data_dir)
    if not path.is_file():
        return {"schema": SCHEMA, "drives": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(f"挂载配置无法读取：{path}（{exc}）。") from exc
    if not isinstance(value, dict) or not isinstance(value.get("drives"), list):
        raise ValueError(f"挂载配置格式不受支持：{path}。")
    value["schema"] = SCHEMA
    return value


def save_config(data_dir, state):
    atomic_write(config_path(data_dir), json.dumps(state, ensure_ascii=False, indent=2))


def _text(value, limit=80):
    return str(value or "").strip()[:limit]


def _port(value, kind):
    if value in (None, ""):
        return DEFAULT_PORTS[kind]
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("端口必须是 1-65535 的数字")
    if not 1 <= port <= 65535:
        raise ValueError("端口必须是 1-65535 的数字")
    return port


def _remote_path(value):
    raw = str(value or "").strip() or "/"
    if "\\" in raw or ":" in raw or "\x00" in raw:
        raise ValueError("远端路径必须是 / 开头的 POSIX 路径，例如 /home/user")
    if not raw.startswith("/"):
        raw = "/" + raw
    parts = PurePosixPath(raw).parts
    if any(part in ("..", ".") for part in parts):
        raise ValueError("远端路径不能包含 .. 或 .")
    return str(PurePosixPath(*parts)) if len(parts) > 1 else "/"


def _drive_letter(value):
    letter = str(value or "").strip().rstrip(":").upper()
    if not re.fullmatch(r"[A-Z]", letter):
        raise ValueError("盘符必须是单个字母 A-Z，例如 X")
    if letter == "C":
        raise ValueError("不能使用系统盘 C:")
    return letter


def normalize_entry(params, old=None):
    kind = _text(params.get("kind"), 10).lower()
    if kind not in KINDS:
        raise ValueError("类型只支持 sftp 或 webdav")
    name = _text(params.get("name"), 80)
    if not name:
        raise ValueError("请填写名称")
    host = _text(params.get("host"), 253)
    port = params.get("port")
    tls = bool(params.get("tls", (old or {}).get("tls", False)))
    if kind == "webdav" and re.fullmatch(r"https?://[^/]+", host):
        parsed = urlparse(host)
        host = parsed.hostname or ""
        port = parsed.port
        tls = parsed.scheme == "https"
    if not host or re.search(r"[/\\\s:]", host):
        raise ValueError("主机名/IP 不能包含空格或 / \\ : 等字符")
    entry = {
        "id": (old or {}).get("id") or uuid.uuid4().hex,
        "kind": kind,
        "name": name,
        "host": host,
        "port": _port(port, kind),
        "username": _text(params.get("username"), 80),
        "remote_path": _remote_path(params.get("remote_path") or (old or {}).get("remote_path") or "/"),
        "drive_letter": _drive_letter(params.get("drive_letter") or (old or {}).get("drive_letter")),
        "read_only": bool(params.get("read_only", (old or {}).get("read_only", False))),
        "auto_mount": bool(params.get("auto_mount", (old or {}).get("auto_mount", False))),
        "volname": _text(params.get("volname"), 32) or name,
        "cache_mode": _text(params.get("cache_mode"), 10) or (old or {}).get("cache_mode") or "writes",
        "tls": tls,
        "insecure_tls": bool(params.get("insecure_tls", (old or {}).get("insecure_tls", False))),
        "webdav_vendor": _text(params.get("webdav_vendor"), 20) or (old or {}).get("webdav_vendor") or "other",
        "created_at": (old or {}).get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if entry["cache_mode"] not in CACHE_MODES:
        raise ValueError("缓存模式只支持 off / minimal / writes / full")
    return entry


def apply_password(state, entry, params):
    """RaiDrive 语义：留空密码保留旧值，除非端点/账户变化或显式清除。"""
    old = next((item for item in state["drives"] if item["id"] == entry["id"]), None)
    cleared = bool(params.get("clear_password"))
    provided = params.get("password")
    if provided is not None and str(provided) != "":
        entry["password_dpapi"] = protect(str(provided))
        return
    identity = (entry["kind"], entry["host"], entry["port"], entry["username"])
    if old is None or cleared or identity != (old["kind"], old["host"], old["port"], old["username"]):
        entry.pop("password_dpapi", None)
    elif old.get("password_dpapi"):
        entry["password_dpapi"] = old["password_dpapi"]


class MountRuntime:
    """One supervised rclone child per configured drive."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.processes = {}       # id -> Popen
        self.stopping = set()
        self.threads = {}
        self.credentials = {}     # token -> {"password": str, "expires": float}
        self._states = {}         # id -> {"state", "message", "restarts"}

    def state(self, entry_id):
        with self.lock:
            return self._states.setdefault(entry_id, {"state": "stopped", "message": "", "restarts": 0})

    def set_state(self, entry_id, state, message=""):
        with self.lock:
            record = self.state(entry_id)
            record["state"] = state
            record["message"] = message
        self.app.emit("netmount.state", id=entry_id, state=state, message=message)

    def is_running(self, entry_id):
        with self.lock:
            process = self.processes.get(entry_id)
        return process is not None and process.poll() is None

    def put_credential(self, password):
        token = uuid.uuid4().hex
        with self.lock:
            for key in [k for k, v in self.credentials.items() if v["expires"] < time.monotonic()]:
                self.credentials.pop(key, None)
            self.credentials[token] = {"password": str(password), "expires": time.monotonic() + CREDENTIAL_TTL_SECONDS}
        return token

    def take_credential(self, token):
        with self.lock:
            record = self.credentials.pop(str(token or ""), None)
        if record is None or record["expires"] < time.monotonic():
            raise ValueError("凭据已过期，请重新提交")
        return record["password"]

    def kill(self, entry_id):
        with self.lock:
            process = self.processes.pop(entry_id, None)
        if process is None or process.poll() is not None:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, creationflags=flags)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def stop(self, entry_id):
        with self.lock:
            self.stopping.add(entry_id)
        try:
            self.kill(entry_id)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not self.is_running(entry_id):
                    break
                time.sleep(0.1)
            self.set_state(entry_id, "stopped")
        finally:
            with self.lock:
                self.stopping.discard(entry_id)

    def stop_all(self):
        with self.lock:
            ids = [entry_id for entry_id, process in self.processes.items() if process.poll() is None]
        for entry_id in ids:
            self.stop(entry_id)

    def log_tail(self, entry_id):
        path = log_dir(self.app.data_dir) / f"{entry_id}.log"
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - LOG_TAIL_BYTES))
                return handle.read(LOG_TAIL_BYTES).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def runtime_view(self, entry):
        record = self.state(entry["id"])
        return {"state": record["state"], "message": record["message"],
                "restarts": record["restarts"], "log_tail": self.log_tail(entry["id"])}


def obscure_password(engine, password):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run([str(engine), "obscure", "-"], input=str(password) + "\n",
                            capture_output=True, text=True, timeout=30, creationflags=flags)
    output = (result.stdout or "").strip()
    if result.returncode or not output:
        raise ValueError("rclone obscure 失败：" + (result.stderr or "")[-300:])
    return output


def build_environment(entry, password, engine):
    """Rclone reads the per-mount remote from RCLONE_CONFIG_* environment variables,
    keeping credentials out of argv entirely."""
    remote = "NM" + entry["id"][:8].upper()
    env = dict(os.environ)
    env[f"RCLONE_CONFIG_{remote}_TYPE"] = entry["kind"]
    if entry["kind"] == "sftp":
        env[f"RCLONE_CONFIG_{remote}_HOST"] = entry["host"]
        env[f"RCLONE_CONFIG_{remote}_PORT"] = str(entry["port"])
    else:
        # The connection origin only; the remote path travels in the mount
        # source (`remote:<path>`), otherwise it would be applied twice.
        scheme = "https" if entry["tls"] else "http"
        env[f"RCLONE_CONFIG_{remote}_URL"] = f"{scheme}://{entry['host']}:{entry['port']}"
        env[f"RCLONE_CONFIG_{remote}_VENDOR"] = entry["webdav_vendor"]
    if entry["username"]:
        env[f"RCLONE_CONFIG_{remote}_USER"] = entry["username"]
    if password:
        env[f"RCLONE_CONFIG_{remote}_PASS"] = obscure_password(engine, password)
    if entry.get("insecure_tls"):
        env["RCLONE_NO_CHECK_CERTIFICATE"] = "true"
    return remote, env


def mount_arguments(entry, remote, log_path):
    argv = [str(engine_path()), "mount", f"{remote}:{entry['remote_path']}", entry["drive_letter"] + ":"]
    argv += ["--volname", entry["volname"] or entry["name"], "--network-mode", "--links",
             "--vfs-cache-mode", entry["cache_mode"], "--dir-cache-time", "10s"]
    if entry.get("read_only"):
        argv.append("--read-only")
    argv += ["--log-file", str(log_path), "--log-level", "INFO"]
    return argv


def spawn_mount(entry, password, app, prepared=None):
    engine = engine_path()
    if not engine.is_file():
        raise ValueError("找不到挂载引擎 rclone.exe；请重新安装或更新工具箱便携版")
    if not winfsp_installed():
        raise ValueError("尚未安装 WinFsp 用户态文件系统驱动。请从 https://winfsp.dev/rel/ 下载安装后重试。")
    letter = entry["drive_letter"]
    if letter in taken_letters():
        raise ValueError(f"盘符 {letter}: 已被系统或其他程序占用，请换一个盘符")
    remote, env = prepared if prepared is not None else build_environment(entry, password, engine)
    logs = log_dir(app.data_dir)
    logs.mkdir(parents=True, exist_ok=True)
    argv = mount_arguments(entry, remote, logs / f"{entry['id']}.log")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE, belt and braces next to CREATE_NO_WINDOW
    process = subprocess.Popen(argv, cwd=str(logs), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=flags, startupinfo=startup)
    with app.netmount_runtime.lock:
        app.netmount_runtime.processes[entry["id"]] = process
    return process


def wait_ready(entry, process, probe=None, check_cancelled=None, timeout=READY_TIMEOUT_SECONDS):
    """Poll until the letter answers; a dead child aborts immediately."""
    deadline = time.monotonic() + timeout
    letter = entry["drive_letter"] + ":\\"
    while time.monotonic() < deadline:
        if check_cancelled:
            check_cancelled()
        if process.poll() is not None:
            return False
        if probe is None:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            bit = ord(entry["drive_letter"]) - ord("A")
            if mask >> bit & 1 and os.path.exists(letter):
                return True
        elif probe():
            return True
        time.sleep(0.4)
    return False


def supervise(app, entry, restarts=0):
    """Child watchdog: restart on late exits, give up on fast crashes."""
    runtime = app.netmount_runtime
    while True:
        if entry["id"] in runtime.stopping:
            return
        with runtime.lock:
            process = runtime.processes.get(entry["id"])
        if process is None:
            return
        started = time.monotonic()
        code = process.wait()
        with runtime.lock:
            if entry["id"] in runtime.stopping or runtime.processes.get(entry["id"]) is not process:
                return
            runtime.processes.pop(entry["id"], None)
        if entry["id"] in runtime.stopping:
            return
        uptime = time.monotonic() - started
        if uptime < RESTART_GRACE_SECONDS:
            detail = runtime.log_tail(entry["id"]).strip().splitlines()
            hint = detail[-1][:300] if detail else f"退出码 {code}"
            runtime.set_state(entry["id"], "error", f"挂载进程启动失败：{hint}")
            return
        if code == 0:
            runtime.set_state(entry["id"], "stopped")
            return
        restarts = restarts + 1 if uptime < RESTART_RESET_SECONDS else 1
        if restarts > RESTART_LIMIT:
            runtime.set_state(entry["id"], "error", f"反复断开（已重试 {RESTART_LIMIT} 次），已停止自动重连")
            return
        runtime.set_state(entry["id"], "reconnecting", f"连接断开，{RESTART_DELAY_SECONDS} 秒后重连（第 {restarts} 次）")
        time.sleep(RESTART_DELAY_SECONDS)
        if entry["id"] in runtime.stopping:
            return
        try:
            saved = entry.get("password_dpapi")
            password = protect(saved, decrypt=True) if saved else ""
            process = spawn_mount(entry, password, app)
        except ValueError as exc:
            runtime.set_state(entry["id"], "error", str(exc))
            return
        runtime.set_state(entry["id"], "mounted", f"自动重连成功（第 {restarts} 次）")


def probe_remote(engine, remote, entry, env, timeout=30):
    """One real directory read: a mount that cannot read its root is not a mount."""
    argv = [str(engine), "lsf", f"{remote}:{entry['remote_path']}", "--max-depth", "1"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                env=env, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise ValueError(f"连接超时：服务器在 {timeout} 秒内没有响应")
    if result.returncode:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        hint = lines[-1][:400] if lines else f"退出码 {result.returncode}"
        if "401" in hint or "Unauthorized" in hint or "unable to authenticate" in hint or "No supported methods remain" in hint:
            raise ValueError(f"认证失败，请检查用户名和密码（服务器返回：{hint}）")
        if "403" in hint or "Forbidden" in hint:
            raise ValueError(f"服务器拒绝访问（403），请检查账号权限（{hint}）")
        if "connection refused" in hint or "no route" in hint or "timeout" in hint or "unreachable" in hint:
            raise ValueError(f"无法连接服务器，请检查地址、端口和网络（{hint}）")
        raise ValueError(hint)
    return [line for line in (result.stdout or "").splitlines() if line][:5]


def mount_job(app, job):
    entry_id = str(job.params.get("id") or "")
    state = load_config(app.data_dir)
    entry = next((item for item in state["drives"] if item["id"] == entry_id), None)
    if entry is None:
        raise ValueError("挂载条目不存在，可能已被删除")
    runtime = app.netmount_runtime
    if runtime.is_running(entry_id):
        raise ValueError("该挂载已在运行，请先卸载")
    engine = engine_path()
    if not engine.is_file():
        raise ValueError("找不到挂载引擎 rclone.exe；请重新安装或更新工具箱便携版")
    token = job.params.get("credential_token")
    if token:
        password = runtime.take_credential(token)
    else:
        saved = entry.get("password_dpapi")
        password = protect(saved, decrypt=True) if saved else ""
    prepared = build_environment(entry, password, engine)
    job.progress(10, "启动挂载进程")
    process = spawn_mount(entry, password, app, prepared)
    job.progress(30, f"等待盘符 {entry['drive_letter']}: 就绪")
    try:
        ready = wait_ready(entry, process, check_cancelled=job.check_cancelled)
    except BaseException:
        runtime.kill(entry_id)
        raise
    if not ready:
        runtime.kill(entry_id)
        detail = runtime.log_tail(entry_id).strip().splitlines()
        hint = detail[-1][:400] if detail else f"退出码 {process.poll()}"
        runtime.set_state(entry_id, "error",
                          f"挂载失败：盘符 {entry['drive_letter']}: 未在 {READY_TIMEOUT_SECONDS} 秒内出现。{hint}")
        raise ValueError(f"挂载失败：盘符 {entry['drive_letter']}: 未在 {READY_TIMEOUT_SECONDS} 秒内出现。{hint}")
    job.progress(70, "正在验证连接")
    remote, env = prepared
    try:
        probe_remote(engine, remote, entry, env)
    except ValueError as exc:
        runtime.kill(entry_id)
        runtime.set_state(entry_id, "error", f"挂载后验证失败：{exc}")
        raise ValueError(f"挂载后验证失败：{exc}") from exc
    runtime.set_state(entry_id, "mounted")
    thread = threading.Thread(target=supervise, args=(app, entry), name=f"netmount-{entry_id}", daemon=True)
    with runtime.lock:
        runtime.threads[entry_id] = thread
    thread.start()
    job.progress(100, f"已挂载到 {entry['drive_letter']}:")
    return {"id": entry_id, "drive_letter": entry["drive_letter"], "state": "mounted"}


def unmount_job(app, job):
    entry_id = str(job.params.get("id") or "")
    runtime = app.netmount_runtime
    if not runtime.is_running(entry_id):
        runtime.set_state(entry_id, "stopped")
        return {"id": entry_id, "state": "stopped"}
    job.progress(30, "正在停止挂载进程")
    runtime.stop(entry_id)
    job.progress(100, "已卸载")
    return {"id": entry_id, "state": "stopped"}


def test_job(app, job):
    entry_id = str(job.params.get("id") or "")
    state = load_config(app.data_dir)
    entry = next((item for item in state["drives"] if item["id"] == entry_id), None)
    if entry is None:
        raise ValueError("挂载条目不存在，可能已被删除")
    engine = engine_path()
    if not engine.is_file():
        raise ValueError("找不到挂载引擎 rclone.exe；请重新安装或更新工具箱便携版")
    runtime = app.netmount_runtime
    token = job.params.get("credential_token")
    if token:
        password = runtime.take_credential(token)
    else:
        saved = entry.get("password_dpapi")
        password = protect(saved, decrypt=True) if saved else ""
    remote, env = build_environment(entry, password, engine)
    job.progress(40, "正在连接")
    sample = probe_remote(engine, remote, entry, env)
    job.progress(100, "连接成功")
    return {"ok": True, "message": "连接成功", "sample": sample}


def status(app):
    state = load_config(app.data_dir)
    runtime = app.netmount_runtime
    drives = [public_entry(entry, runtime.runtime_view(entry)) for entry in state["drives"]]
    return {
        "engine": {"available": engine_available(), "path": str(engine_path()), "version": engine_version()},
        "winfsp": {"available": winfsp_installed(), "download_url": WINFSP_URL},
        "taken_letters": sorted(taken_letters()),
        "drives": drives,
    }


def public_entry(entry, runtime):
    view = {key: value for key, value in entry.items() if not key.endswith("_dpapi")}
    view["has_password"] = bool(entry.get("password_dpapi"))
    view.update(runtime)
    view["mounted"] = view["state"] == "mounted"
    return view


def save(app, params):
    params = dict(params)
    if not params.get("password") and params.get("reuse_webdav_password"):
        password = webdav_sync_password(app)
        if password:
            params["password"] = password
    state = load_config(app.data_dir)
    entry_id = params.get("id")
    old = next((item for item in state["drives"] if item["id"] == entry_id), None) if entry_id else None
    if entry_id and old is None:
        raise ValueError("挂载条目不存在，可能已被删除")
    runtime = app.netmount_runtime
    if old and runtime.is_running(old["id"]):
        raise ValueError("该挂载正在运行，请先卸载再修改")
    entry = normalize_entry(params, old)
    conflict = next((item for item in state["drives"]
                     if item["id"] != entry["id"] and item["drive_letter"] == entry["drive_letter"]), None)
    if conflict:
        raise ValueError(f"盘符 {entry['drive_letter']}: 已被「{conflict['name']}」使用")
    if old is None and len(state["drives"]) >= MAX_DRIVES:
        raise ValueError(f"最多保存 {MAX_DRIVES} 条挂载")
    apply_password(state, entry, params)
    if old:
        state["drives"] = [entry if item["id"] == entry["id"] else item for item in state["drives"]]
    else:
        state["drives"].append(entry)
    save_config(app.data_dir, state)
    return {"id": entry["id"], "drives": status(app)["drives"]}


def remove(app, params):
    entry_id = str(params.get("id") or "")
    state = load_config(app.data_dir)
    entry = next((item for item in state["drives"] if item["id"] == entry_id), None)
    if entry is None:
        raise ValueError("挂载条目不存在，可能已被删除")
    app.netmount_runtime.stop(entry_id)
    state["drives"] = [item for item in state["drives"] if item["id"] != entry_id]
    save_config(app.data_dir, state)
    try:
        (log_dir(app.data_dir) / f"{entry_id}.log").unlink()
    except OSError:
        pass
    return {"drives": status(app)["drives"]}


def mount(app, params):
    params = dict(params)
    if params.get("password") and not params.get("credential_token"):
        params["credential_token"] = app.netmount_runtime.put_credential(params.pop("password"))
    return app.jobs.submit(TOOL, {"id": params.get("id"), "credential_token": params.get("credential_token")})


def unmount(app, params):
    return app.jobs.submit("netmount.unmount", {"id": params.get("id")})


def test(app, params):
    params = dict(params)
    if params.get("password") and not params.get("credential_token"):
        params["credential_token"] = app.netmount_runtime.put_credential(params.pop("password"))
    return app.jobs.submit(TEST_TOOL, {"id": params.get("id"), "credential_token": params.get("credential_token")})


# ---------------------------------------------------------------- RaiDrive import

def parse_address(kind, address):
    address = address.strip()
    try:
        if kind == "sftp":
            match = re.fullmatch(r"sftp://([^/:]+)(?::(\d+))?(/.*)?", address)
            if not match:
                return None
            return {"host": match.group(1), "port": int(match.group(2) or 22),
                    "remote_path": match.group(3) or "/"}
        match = re.fullmatch(r"(https?)://([^/:]+)(?::(\d+))?(/.*)?", address)
        if not match:
            return None
        return {"host": match.group(2), "port": int(match.group(3) or (443 if match.group(1) == "https" else 80)),
                "tls": match.group(1) == "https", "remote_path": match.group(4) or "/"}
    except ValueError:
        return None


def raidrive_candidates(directory=None):
    """Read the RaiDrive service log: everything except passwords is recoverable."""
    directory = Path(directory or RAIDRIVE_LOG_DIRECTORY)
    if not directory.is_dir():
        return []
    kinds, addresses, letters = {}, {}, {}
    for path in sorted(directory.glob("service.log*")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in ENTRY_RE.finditer(text):
            kinds[match.group(2).strip()] = match.group(1).lower()
        for match in AUTH_RE.finditer(text):
            name, address = match.group(1).strip(), match.group(2)
            if name in kinds:
                addresses[name] = address
        for match in INIT_RE.finditer(text):
            name = match.group(1).strip()
            letters[name] = (match.group(2), match.group(3).lower() == "true")
    candidates = []
    for name, kind in kinds.items():
        parsed = parse_address(kind, addresses.get(name, ""))
        if not parsed:
            continue
        letter, read_only = letters.get(name, ("", False))
        candidates.append({
            "key": f"{kind}:{name}", "kind": kind, "name": name,
            "host": parsed["host"], "port": parsed["port"], "tls": parsed.get("tls", False),
            "remote_path": parsed["remote_path"], "drive_letter": letter, "read_only": read_only,
        })
    return candidates


def import_raidrive(app, params=None):
    candidates = raidrive_candidates(params.get("directory") if params else None)
    state = load_config(app.data_dir)
    existing = {(item["kind"], item["host"], item["port"], item["remote_path"]) for item in state["drives"]}
    for candidate in candidates:
        candidate["exists"] = (candidate["kind"], candidate["host"], candidate["port"], candidate["remote_path"]) in existing
    return {"candidates": candidates, "log_directory": str(RAIDRIVE_LOG_DIRECTORY)}


def import_raidrive_apply(app, params):
    candidates = params.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("请选择要导入的挂载")
    passwords = params.get("passwords") or {}
    if not isinstance(passwords, dict):
        raise ValueError("密码表格式不正确")
    results = []
    for item in candidates[:MAX_DRIVES]:
        if not isinstance(item, dict):
            continue
        save_params = {key: value for key, value in item.items() if key != "exists"}
        password = str(passwords.get(item.get("key"), ""))
        if password:
            save_params["password"] = password
        try:
            record = save(app, save_params)
            results.append({"key": item.get("key"), "id": record["id"], "ok": True})
        except (ValueError, RuntimeError) as exc:
            results.append({"key": item.get("key"), "ok": False, "error": str(exc)})
    if not results:
        raise ValueError("没有可导入的挂载")
    failed = [item for item in results if not item["ok"]]
    return {"results": results, "imported": len(results) - len(failed),
            "drives": status(app)["drives"], "error": failed[0]["error"] if failed else ""}


def import_webdav_sync(app, params=None):
    """Offer the WebDAV snapshot connection as a mount candidate (fills the form)."""
    path = Path(app.data_dir) / "webdav.json"
    saved = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    url = str(saved.get("url") or "")
    if not url:
        return {"candidate": None}
    parsed = urlparse(url)
    return {"candidate": {
        "kind": "webdav", "name": "WebDAV 同步",
        "host": parsed.hostname or "", "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "tls": parsed.scheme == "https", "username": str(saved.get("username") or ""),
        "remote_path": _remote_path(saved.get("remote_path")), "drive_letter": "",
        "has_password": bool(saved.get("password_dpapi")),
    }}


def webdav_sync_password(app):
    """Reuse the saved WebDAV sync password for an imported mount, if present."""
    path = Path(app.data_dir) / "webdav.json"
    saved = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    blob = saved.get("password_dpapi")
    return protect(blob, decrypt=True) if blob else ""


def auto_mount_all(app):
    state = load_config(app.data_dir)
    for entry in state["drives"]:
        if not entry.get("auto_mount") or app.netmount_runtime.is_running(entry["id"]):
            continue
        try:
            app.jobs.submit(TOOL, {"id": entry["id"]})
        except (ValueError, RuntimeError):
            continue


def close(app):
    app.netmount_runtime.stop_all()


def register(app):
    app.netmount_runtime = MountRuntime(app)

    app.jobs.register(TOOL, lambda job: mount_job(app, job))
    app.jobs.register("netmount.unmount", lambda job: unmount_job(app, job))
    app.jobs.register(TEST_TOOL, lambda job: test_job(app, job))
    app.register("netmount.status", lambda p: status(app))
    app.register("netmount.save", lambda p: save(app, p))
    app.register("netmount.remove", lambda p: remove(app, p))
    app.register("netmount.mount", lambda p: mount(app, p))
    app.register("netmount.unmount", lambda p: unmount(app, p))
    app.register("netmount.test", lambda p: test(app, p))
    app.register("netmount.import_raidrive", lambda p: import_raidrive(app, p))
    app.register("netmount.import_raidrive.apply", lambda p: import_raidrive_apply(app, p))
    app.register("netmount.import_webdav_sync", lambda p: import_webdav_sync(app))

    app.netmount_close = lambda: close(app)

    def delayed_auto_mount():
        time.sleep(2)
        try:
            auto_mount_all(app)
        except (ValueError, OSError, RuntimeError):
            pass

    threading.Thread(target=delayed_auto_mount, name="netmount-automount", daemon=True).start()
