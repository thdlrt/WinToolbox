"""Net mount: config CRUD, RaiDrive semantics, spawn guards, and log import."""
import time
import types
import uuid
from pathlib import Path

import pytest
from toolbox.features import netmount


class FakeJobs:
    def __init__(self, app):
        self.app = app
        self.runners = {}
        self.runs = []

    def register(self, tool, runner):
        self.runners[tool] = runner

    def submit(self, tool, params):
        assert tool in self.runners, f"job {tool} not registered"
        job = types.SimpleNamespace(params=params or {}, progress=lambda *a, **k: None,
                                    check_cancelled=lambda: None)
        self.runs.append((tool, params or {}))
        return self.runners[tool](job)


class FakeApp:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.handlers = {}
        self.events = []
        self.emit = lambda type, **fields: self.events.append({"type": type, **fields})
        self.netmount_runtime = netmount.MountRuntime(self)
        self.jobs = FakeJobs(self)

    def register(self, name, handler):
        self.handlers[name] = handler


@pytest.fixture
def app(tmp_path):
    instance = FakeApp(tmp_path / "data")
    netmount.register(instance)
    instance.jobs.runs.clear()
    return instance


def drive_from(result, name):
    return next(item for item in result["drives"] if item["name"] == name)


def save(app, **params):
    return netmount.save(app, {"kind": "sftp", "name": "NAS", "host": "192.168.1.5",
                               "drive_letter": "X", **params})


def test_save_normalizes_and_round_trips(app):
    record = save(app, name="  NAS  ", port="2200", username="root", password="pw",
                  remote_path="srv/", drive_letter="x:")
    assert record["id"]
    entry = drive_from(record, "NAS")
    assert entry["kind"] == "sftp" and entry["host"] == "192.168.1.5" and entry["port"] == 2200
    assert entry["remote_path"] == "/srv" and entry["drive_letter"] == "X"
    assert entry["has_password"] and not any(key.endswith("_dpapi") for key in entry)
    # persisted config keeps only DPAPI ciphertext
    stored = netmount.load_config(app.data_dir)["drives"][0]
    assert set(stored) & {"password"} == set()


def test_save_rejects_bad_input(app):
    for params in (
        {"kind": "ftp"}, {"name": ""}, {"host": "a b"}, {"host": "a/b"}, {"port": "0"},
        {"port": "abc"}, {"drive_letter": "C"}, {"drive_letter": "XY"}, {"remote_path": "../x"},
        {"remote_path": "C:/x"}, {"cache_mode": "turbo"},
    ):
        with pytest.raises(ValueError):
            save(app, **params)
    assert netmount.load_config(app.data_dir)["drives"] == []


def test_drive_letter_conflict(app):
    save(app, name="A", drive_letter="X")
    with pytest.raises(ValueError, match="已被"):
        save(app, name="B", drive_letter="x")


def test_empty_password_keeps_saved_value_until_endpoint_changes(app):
    record = save(app, name="NAS", username="root", password="first")
    entry_id = record["id"]
    record = netmount.save(app, {"kind": "sftp", "name": "NAS", "host": "192.168.1.5",
                                 "drive_letter": "X", "id": entry_id, "username": "root",
                                 "remote_path": "/data"})
    entry = drive_from(record, "NAS")
    assert entry["id"] == entry_id and entry["remote_path"] == "/data"
    assert entry["has_password"], "same endpoint/user keeps the saved password"
    record = netmount.save(app, {"kind": "sftp", "name": "NAS", "host": "192.168.1.5",
                                 "drive_letter": "X", "id": entry_id, "username": "admin"})
    entry = drive_from(record, "NAS")
    assert not entry["has_password"], "changing the account clears the stale password"
    record = netmount.save(app, {"kind": "sftp", "name": "NAS", "host": "192.168.1.5",
                                 "drive_letter": "X", "id": entry_id, "username": "admin",
                                 "password": "second"})
    assert drive_from(record, "NAS")["has_password"]
    record = netmount.save(app, {"kind": "sftp", "name": "NAS", "host": "192.168.1.5",
                                 "drive_letter": "X", "id": entry_id, "username": "admin",
                                 "clear_password": True})
    assert not drive_from(record, "NAS")["has_password"]
    with pytest.raises(ValueError, match="已被"):
        save(app, name="NAS", username="root")  # a new entry cannot reuse the letter


def test_save_rejects_mounted_entry(app, monkeypatch):
    record = save(app, name="NAS")
    monkeypatch.setattr(app.netmount_runtime, "is_running", lambda entry_id: True)
    with pytest.raises(ValueError, match="正在运行"):
        save(app, id=record["id"], name="NAS2")
    with pytest.raises(ValueError, match="不存在"):
        save(app, id="missing", name="NAS2")


def test_remove_stops_and_deletes(app, monkeypatch):
    record = save(app, name="NAS")
    stopped = []
    monkeypatch.setattr(app.netmount_runtime, "stop", lambda entry_id: stopped.append(entry_id))
    result = netmount.remove(app, {"id": record["id"]})
    assert stopped == [record["id"]]
    assert result["drives"] == []
    with pytest.raises(ValueError, match="不存在"):
        netmount.remove(app, {"id": record["id"]})


def test_spawn_requires_engine_and_winfsp(app, monkeypatch, tmp_path):
    record = save(app, name="NAS")
    entry = netmount.load_config(app.data_dir)["drives"][0]
    monkeypatch.setattr(netmount, "engine_path", lambda: tmp_path / "missing.exe")
    with pytest.raises(ValueError, match="rclone"):
        netmount.spawn_mount(entry, "", app)
    engine = tmp_path / "rclone.exe"
    engine.write_bytes(b"stub")
    monkeypatch.setattr(netmount, "engine_path", lambda: engine)
    monkeypatch.setattr(netmount, "winfsp_installed", lambda: False)
    with pytest.raises(ValueError, match="WinFsp"):
        netmount.spawn_mount(entry, "", app)


def test_spawn_rejects_taken_letter(app, monkeypatch, tmp_path):
    record = save(app, name="NAS", drive_letter="Q")
    entry = netmount.load_config(app.data_dir)["drives"][0]
    engine = tmp_path / "rclone.exe"
    engine.write_bytes(b"stub")
    monkeypatch.setattr(netmount, "engine_path", lambda: engine)
    monkeypatch.setattr(netmount, "winfsp_installed", lambda: True)
    monkeypatch.setattr(netmount, "taken_letters", lambda: {"Q"})
    with pytest.raises(ValueError, match="Q:"):
        netmount.spawn_mount(entry, "", app)


def test_mount_job_round_trip(app, monkeypatch, tmp_path):
    record = save(app, name="NAS", password="pw")
    entry_id = record["id"]
    spawned = []
    engine = tmp_path / "rclone.exe"
    engine.write_bytes(b"stub")
    monkeypatch.setattr(netmount, "engine_path", lambda: engine)
    monkeypatch.setattr(netmount, "winfsp_installed", lambda: True)
    monkeypatch.setattr(netmount, "taken_letters", lambda: set())
    monkeypatch.setattr(netmount, "obscure_password", lambda engine, password: "OBSCURED")
    monkeypatch.setattr(netmount, "probe_remote", lambda *args, **kwargs: ["ok"])

    class FakeProcess:
        def __init__(self):
            self.pid = 4242
        def poll(self):
            return None
        def wait(self, timeout=None):
            time.sleep(60)  # stays alive for the whole test; supervise keeps watching
            return None

    def fake_spawn(entry, password, app, prepared=None):
        spawned.append((entry["id"], password))
        process = FakeProcess()
        app.netmount_runtime.processes[entry["id"]] = process
        return process

    monkeypatch.setattr(netmount, "spawn_mount", fake_spawn)
    monkeypatch.setattr(netmount, "wait_ready", lambda entry, process, probe=None, check_cancelled=None, timeout=30: True)
    result = app.jobs.submit(netmount.TOOL, {"id": entry_id})
    assert result == {"id": entry_id, "drive_letter": "X", "state": "mounted"}
    assert spawned and spawned[0][0] == entry_id and spawned[0][1] == "pw"
    status = netmount.status(app)
    assert drive_from(status, "NAS")["state"] == "mounted"
    assert drive_from(status, "NAS")["mounted"]

    stopped = []
    monkeypatch.setattr(app.netmount_runtime, "stop", lambda entry_id: stopped.append(entry_id))
    app.jobs.submit("netmount.unmount", {"id": entry_id})
    assert stopped == [entry_id]


def test_mount_verifies_root_and_unmounts_on_auth_failure(app, monkeypatch, tmp_path):
    record = save(app, name="NAS", password="wrong")
    entry_id = record["id"]
    engine = tmp_path / "rclone.exe"
    engine.write_bytes(b"stub")
    monkeypatch.setattr(netmount, "engine_path", lambda: engine)
    monkeypatch.setattr(netmount, "winfsp_installed", lambda: True)
    monkeypatch.setattr(netmount, "taken_letters", lambda: set())
    monkeypatch.setattr(netmount, "obscure_password", lambda engine, password: "OBSCURED")
    killed = []
    monkeypatch.setattr(app.netmount_runtime, "kill", lambda entry_id: killed.append(entry_id))
    monkeypatch.setattr(netmount, "spawn_mount",
                        lambda entry, password, app, prepared=None: type("P", (), {"pid": 5, "poll": lambda self: None})())
    monkeypatch.setattr(netmount, "wait_ready", lambda entry, process, probe=None, check_cancelled=None, timeout=30: True)

    def rejected(*args, **kwargs):
        raise ValueError("认证失败，请检查用户名和密码（服务器返回：401 Unauthorized）")

    monkeypatch.setattr(netmount, "probe_remote", rejected)
    with pytest.raises(ValueError, match="挂载后验证失败.*认证失败"):
        app.jobs.submit(netmount.TOOL, {"id": entry_id})
    assert killed == [entry_id]
    view = drive_from(netmount.status(app), "NAS")
    assert view["state"] == "error" and "验证失败" in view["message"]


def test_mount_failure_reports_log_hint(app, monkeypatch, tmp_path):
    record = save(app, name="NAS")
    entry_id = record["id"]
    engine = tmp_path / "rclone.exe"
    engine.write_bytes(b"stub")
    monkeypatch.setattr(netmount, "engine_path", lambda: engine)
    monkeypatch.setattr(netmount, "winfsp_installed", lambda: True)
    monkeypatch.setattr(netmount, "taken_letters", lambda: set())

    class Dead:
        pid = 7
        def poll(self):
            return 5

    monkeypatch.setattr(netmount, "spawn_mount",
                        lambda entry, password, app, prepared=None: Dead())
    with pytest.raises(ValueError, match="未在"):
        app.jobs.submit(netmount.TOOL, {"id": entry_id})
    view = drive_from(netmount.status(app), "NAS")
    assert view["state"] == "error" and "挂载失败" in view["message"]


def test_supervise_gives_up_on_fast_crash(app, monkeypatch):
    record = save(app, name="NAS")
    entry = netmount.load_config(app.data_dir)["drives"][0]
    entry_id = entry["id"]

    class Fast:
        pid = 9
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 3

    runtime = app.netmount_runtime
    runtime.processes[entry_id] = Fast()
    runtime.threads = {}
    netmount.supervise(app, entry)
    assert runtime.state(entry_id)["state"] == "error"
    assert "启动失败" in runtime.state(entry_id)["message"]


def test_supervise_restarts_with_saved_password(app, monkeypatch):
    record = save(app, name="NAS", password="pw")
    entry = netmount.load_config(app.data_dir)["drives"][0]
    entry_id = entry["id"]
    monkeypatch.setattr(netmount, "RESTART_DELAY_SECONDS", 0)
    monkeypatch.setattr(netmount, "RESTART_GRACE_SECONDS", 0)
    monkeypatch.setattr(netmount, "winfsp_installed", lambda: True)
    monkeypatch.setattr(netmount, "taken_letters", lambda: set())
    calls = []

    class Flaky:
        pid = 11
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 9

    def spawn(entry, password, app):
        calls.append(password)
        if len(calls) > 1:
            raise ValueError("engine vanished")
        process = Flaky()
        app.netmount_runtime.processes[entry["id"]] = process
        return process

    monkeypatch.setattr(netmount, "spawn_mount", spawn)
    app.netmount_runtime.processes[entry_id] = Flaky()
    netmount.supervise(app, entry)
    assert calls == ["pw", "pw"], "restarts reuse the saved DPAPI password"
    assert app.netmount_runtime.state(entry_id)["state"] == "error"
    assert "engine vanished" in app.netmount_runtime.state(entry_id)["message"]


def test_wait_ready_probe_and_dead_child():
    entry = {"drive_letter": "W"}

    class Alive:
        def poll(self):
            return None

    class Dead:
        def poll(self):
            return 1

    assert netmount.wait_ready(entry, Alive(), probe=lambda: True, timeout=1)
    assert not netmount.wait_ready(entry, Dead(), probe=lambda: False, timeout=1)
    with pytest.raises(AssertionError):
        netmount.wait_ready(entry, Alive(), probe=lambda: False, timeout=0.5, check_cancelled=lambda: (_ for _ in ()).throw(AssertionError()))


def test_environment_hides_password_from_argv(app, monkeypatch, tmp_path):
    record = save(app, name="NAS", password="sekret", username="root")
    entry = netmount.load_config(app.data_dir)["drives"][0]
    engine = tmp_path / "rclone.exe"
    engine.write_bytes(b"stub")
    monkeypatch.setattr(netmount, "obscure_password", lambda engine, password: "OBSCURED")
    remote, env = netmount.build_environment(entry, "sekret", engine)
    assert env[f"RCLONE_CONFIG_{remote}_TYPE"] == "sftp"
    assert env[f"RCLONE_CONFIG_{remote}_PASS"] == "OBSCURED"
    assert env[f"RCLONE_CONFIG_{remote}_USER"] == "root"
    argv = netmount.mount_arguments(entry, remote, tmp_path / "log")
    joined = " ".join(argv)
    assert "sekret" not in joined and "OBSCURED" not in joined
    assert "--network-mode" in joined and "--links" in joined and str(tmp_path / "log") in joined


def test_webdav_environment_uses_url_and_tls_flags(app, monkeypatch, tmp_path):
    save(app, kind="webdav", name="NAS", host="nas.local", port=5006, tls=True,
         insecure_tls=True, remote_path="/dav", username="u", password="p")
    entry = netmount.load_config(app.data_dir)["drives"][0]
    engine = tmp_path / "rclone.exe"
    monkeypatch.setattr(netmount, "obscure_password", lambda engine, password: "OBSCURED")
    remote, env = netmount.build_environment(entry, "p", engine)
    # The URL is the connection origin only; the path rides on the mount source.
    assert env[f"RCLONE_CONFIG_{remote}_URL"] == "https://nas.local:5006"
    assert env[f"RCLONE_CONFIG_{remote}_VENDOR"] == "other"
    assert env.get("RCLONE_NO_CHECK_CERTIFICATE") == "true"
    source = netmount.mount_arguments(entry, remote, tmp_path / "log")[2]
    assert source == f"{remote}:/dav"


def test_raidrive_candidates_from_log(tmp_path):
    (tmp_path / "service.log").write_text(
        "2026-10-25 01:30:54.640 [INFO] WebDAV (FnOS) : Drive Created\n"
        "2026-10-25 01:30:54.640 [INFO] SFTP (Ubuntu) : Drive Created\n"
        "2026-10-25 01:30:54.686 [INFO] WebDAV API : FnOS : AUTH CONNECT : ADDRESS : http://192.168.100.246:5005/ : ALTERNATIVE : False\n"
        "2026-10-25 01:30:55.177 [INFO] SFTP API : Ubuntu : AUTH CONNECT : ADDRESS : sftp://192.168.100.139:22/ : ALTERNATIVE : False\n"
        "2026-10-25 01:30:54.922 [INFO] FILESYSTEM : FnOS : INIT DONE : DriveLetter : X:, IsNetwork : True, IsGlobal : False, IsReadOnly : False\n"
        "2026-10-25 01:30:55.294 [INFO] FILESYSTEM : Ubuntu : INIT DONE : DriveLetter : W:, IsNetwork : True, IsGlobal : False, IsReadOnly : True\n",
        encoding="utf-8")
    candidates = netmount.raidrive_candidates(tmp_path)
    by_name = {item["name"]: item for item in candidates}
    assert set(by_name) == {"FnOS", "Ubuntu"}
    assert by_name["FnOS"]["kind"] == "webdav" and by_name["FnOS"]["host"] == "192.168.100.246"
    assert by_name["FnOS"]["port"] == 5005 and by_name["FnOS"]["drive_letter"] == "X"
    assert by_name["Ubuntu"]["kind"] == "sftp" and by_name["Ubuntu"]["port"] == 22
    assert by_name["Ubuntu"]["drive_letter"] == "W" and by_name["Ubuntu"]["read_only"] is True
    assert netmount.raidrive_candidates(tmp_path / "missing") == []


def test_import_raidrive_apply_saves_and_reports(app):
    candidates = [
        {"key": "sftp:Ubuntu", "kind": "sftp", "name": "Ubuntu", "host": "192.168.100.139",
         "port": 22, "remote_path": "/", "drive_letter": "W", "read_only": False, "tls": False},
        {"key": "webdav:FnOS", "kind": "webdav", "name": "FnOS", "host": "192.168.100.246",
         "port": 5005, "remote_path": "/", "drive_letter": "X", "read_only": False, "tls": False},
        {"key": "webdav:Clash", "kind": "webdav", "name": "Clash", "host": "10.0.0.2",
         "port": 80, "remote_path": "/", "drive_letter": "X", "read_only": False, "tls": False},
    ]
    result = netmount.import_raidrive_apply(app, {"candidates": candidates, "passwords": {"sftp:Ubuntu": "pw"}})
    assert result["imported"] == 2 and result["results"][2]["ok"] is False
    assert drive_from(result, "Ubuntu")["has_password"]
    assert not drive_from(result, "FnOS")["has_password"]
    assert "已被" in result["error"]
    with pytest.raises(ValueError, match="请选择"):
        netmount.import_raidrive_apply(app, {"candidates": []})


def test_import_webdav_sync_offers_saved_connection(app):
    assert netmount.import_webdav_sync(app) == {"candidate": None}
    (app.data_dir / "webdav.json").write_text(
        '{"url":"http://192.168.100.246:5005","username":"admin","remote_path":"WinToolbox","password_dpapi":"blob"}',
        encoding="utf-8")
    result = netmount.import_webdav_sync(app)["candidate"]
    assert result["host"] == "192.168.100.246" and result["port"] == 5005
    assert result["remote_path"] == "/WinToolbox" and result["has_password"]


def test_register_wires_everything(app):
    netmount.register(app)
    expected = {"netmount.status", "netmount.save", "netmount.remove", "netmount.mount",
                "netmount.unmount", "netmount.test", "netmount.import_raidrive",
                "netmount.import_raidrive.apply", "netmount.import_webdav_sync"}
    assert expected <= set(app.handlers) if hasattr(app, "handlers") else True
    assert callable(app.netmount_close)
    netmount.close(app)
