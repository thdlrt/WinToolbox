"""The script-tool registry: entry CRUD, shipped-script seeding, and real runs."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from toolbox.app import App
from toolbox.features import script_tools

SKILL_ASSET = Path(script_tools.ASSETS) / "dsh-skill-sync.py"

CODEX = """\
[mcp_servers.MyMcpCenter]
url = "http://192.168.100.139:8124/mcp"

[mcp_servers.qq-mail]
command = "npx"
args = ["-y", "@honest-magic/mail-mcp@1.4.0", "--read-only"]

[mcp_servers.node_repl]
command = "node"
"""


def store(tmp_path):
    return {"state_root": str(tmp_path / "state")}


def make(tmp_path, name="demo.py", body="print('hello')\n"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def wait(app, job_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = app.jobs.get(job_id)
        if record["status"] not in ("queued", "running", "cancelling"):
            return record
        time.sleep(0.02)
    raise AssertionError("job timed out")


def shipped(entries, asset):
    entry, = [item for item in entries if item["builtin_asset"] == asset]
    return entry


def test_seeds_every_shipped_script_once(tmp_path):
    params = store(tmp_path)
    first = script_tools.listing(params)
    assert [entry["builtin_asset"] for entry in first["entries"]] == ["dsh-mcp-sync.py", "dsh-skill-sync.py"]
    assert [entry["name"] for entry in first["entries"]] == ["同步 MCP 到 DSH", "同步技能库到 DSH"]
    entry = shipped(first["entries"], "dsh-mcp-sync.py")
    assert entry["runner"] == "python" and entry["exists"] and entry["runnable"] and entry["openable"]
    assert Path(entry["path"]).is_file() and Path(entry["path"]).parent == Path(first["scripts_dir"])
    assert [item["id"] for item in script_tools.listing(params)["entries"]] == [item["id"] for item in first["entries"]]
    state = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert state["schema"] == 1 and state["seeded"] == ["dsh-mcp-sync.py", "dsh-skill-sync.py"]


def test_removing_an_entry_keeps_its_file_and_never_resurrects_it(tmp_path):
    params = store(tmp_path)
    entry = shipped(script_tools.listing(params)["entries"], "dsh-mcp-sync.py")
    for asset in ("dsh-mcp-sync.py", "dsh-skill-sync.py"):
        script_tools.remove(params | {"id": shipped(script_tools.listing(params)["entries"], asset)["id"]})
    assert script_tools.listing(params)["entries"] == []
    assert Path(entry["path"]).is_file()


def test_shipped_script_refreshes_only_untouched_copies(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(script_tools, "ASSETS", assets)
    (assets / "dsh-mcp-sync.py").write_text("print('v1')\n", encoding="utf-8")
    params = store(tmp_path)
    entry, = script_tools.listing(params)["entries"]
    shipped = Path(entry["path"])
    assert shipped.read_text(encoding="utf-8") == "print('v1')\n"

    (assets / "dsh-mcp-sync.py").write_text("print('v2')\n", encoding="utf-8")
    refreshed, = script_tools.listing(params)["entries"]
    assert shipped.read_text(encoding="utf-8") == "print('v2')\n" and not refreshed["update_available"]

    shipped.write_text("print('mine')\n", encoding="utf-8")
    (assets / "dsh-mcp-sync.py").write_text("print('v3')\n", encoding="utf-8")
    kept, = script_tools.listing(params)["entries"]
    assert kept["update_available"] and shipped.read_text(encoding="utf-8") == "print('mine')\n"

    restored, = script_tools.reset(params | {"id": kept["id"]})["entries"]
    assert shipped.read_text(encoding="utf-8") == "print('v3')\n" and not restored["update_available"]


def test_reset_refuses_user_scripts(tmp_path):
    params = store(tmp_path)
    added = script_tools.add(params | {"path": str(make(tmp_path, "own.py"))})
    with pytest.raises(ValueError, match="只有内置脚本"):
        script_tools.reset(params | {"id": added["id"]})


def test_add_update_and_remove_own_entries(tmp_path):
    params = store(tmp_path)
    script = make(tmp_path, "backup.ps1", "Write-Output hi\n")
    added = script_tools.add(params | {"path": str(script)})
    entry, = [item for item in added["entries"] if item["id"] == added["id"]]
    assert entry["name"] == "backup" and entry["runner"] == "powershell" and not entry["builtin"]
    assert entry["folder"] == str(tmp_path)

    renamed = script_tools.update(params | {"id": entry["id"], "name": "备份脚本", "note": "每晚跑一次"})
    kept, = [item for item in renamed["entries"] if item["id"] == entry["id"]]
    assert (kept["name"], kept["note"]) == ("备份脚本", "每晚跑一次")

    removed = script_tools.remove(params | {"id": entry["id"]})
    assert all(item["id"] != entry["id"] for item in removed["entries"])
    assert script.is_file()


def test_entry_edits_are_persisted_across_calls(tmp_path):
    params = store(tmp_path)
    script = make(tmp_path)
    added = script_tools.add(params | {"path": str(script), "name": "我的脚本", "note": "说明"})
    script_tools.update(params | {"id": added["id"], "name": "改名后"})
    listed, = [item for item in script_tools.listing(params)["entries"] if item["id"] == added["id"]]
    assert (listed["name"], listed["note"]) == ("改名后", "说明")


def test_add_rejects_unsupported_types(tmp_path):
    notes = make(tmp_path, "notes.txt", "hello\n")
    with pytest.raises(ValueError, match="不支持的文件类型"):
        script_tools.add(store(tmp_path) | {"path": str(notes)})


def test_add_rejects_missing_files(tmp_path):
    with pytest.raises(ValueError, match="找不到脚本文件"):
        script_tools.add(store(tmp_path) | {"path": str(tmp_path / "missing.py")})


def test_add_rejects_duplicates_and_unknown_ids_are_reported(tmp_path):
    params = store(tmp_path)
    script = make(tmp_path)
    script_tools.add(params | {"path": str(script)})
    with pytest.raises(ValueError, match="已在列表中"):
        script_tools.add(params | {"path": str(script)})
    for call in (script_tools.update, script_tools.remove, script_tools.reset):
        with pytest.raises(ValueError, match="不存在"):
            call(params | {"id": "nope"})


def test_a_damaged_state_file_says_how_to_recover(tmp_path):
    params = store(tmp_path)
    root = tmp_path / "state"
    root.mkdir()
    (root / "state.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取"):
        script_tools.listing(params)


def test_command_mapping_covers_every_supported_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(script_tools.shutil, "which", lambda name: f"C:/bin/{name}")
    argv, runner = script_tools.command_for(tmp_path / "a.py")
    assert runner == "python" and argv == [script_tools.sys.executable, str(tmp_path / "a.py")]
    argv, runner = script_tools.command_for(tmp_path / "a.ps1")
    assert runner == "powershell" and argv[1:3] == ["-NoProfile", "-ExecutionPolicy"] and argv[-1] == str(tmp_path / "a.ps1")
    argv, runner = script_tools.command_for(tmp_path / "a.cmd")
    assert runner == "cmd" and argv[1] == "/c"
    argv, runner = script_tools.command_for(tmp_path / "a.bat")
    assert runner == "cmd"
    argv, runner = script_tools.command_for(tmp_path / "a.exe")
    assert argv == [str(tmp_path / "a.exe")]
    argv, runner = script_tools.command_for(tmp_path / "a.mjs")
    assert runner == "node" and argv[0].endswith("node")
    with pytest.raises(ValueError, match="不支持的文件类型"):
        script_tools.command_for(tmp_path / "a.txt")


def test_missing_node_is_reported_before_the_job_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(script_tools.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="找不到 node"):
        script_tools.command_for(tmp_path / "a.js")


def test_run_executes_the_shipped_script_and_captures_its_output(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))
    monkeypatch.setenv("DSH_PROFILE", "fixture")
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text(CODEX, encoding="utf-8")
    app = App(tmp_path / "app", register_features=False, register_live=False)
    try:
        script_tools.register(app)
        params = store(tmp_path)
        entry = shipped(app.call("script_tools.list", params)["entries"], "dsh-mcp-sync.py")
        job = app.call("script_tools.run", params | {"id": entry["id"]})
        record = wait(app, job["id"])
        assert record["status"] == "completed", record
        console = app.call("script_tools.console", {"job_id": job["id"]})
        assert console["status"] == "completed" and console["runner"] == "python"
        assert "+ MyMcpCenter（streamable-http）" in console["output"]
        assert "跳过：node_repl" in console["output"]
        patch = tmp_path / "dsh" / "profiles" / "fixture" / "cordis.patch.yml"
        text = patch.read_text(encoding="utf-8")
        assert text.startswith("# >>> dsh-mcp-sync\n") and text.rstrip().endswith("# <<< dsh-mcp-sync")
        assert 'serverName: "qq-mail"' in text and 'args: ["-y", "@honest-magic/mail-mcp@1.4.0", "--read-only"]' in text
        assert text.count("dsh-mcp-sync") == 2  # start and end marker
        reloaded = shipped(app.call("script_tools.list", params)["entries"], "dsh-mcp-sync.py")
        assert reloaded["last_job_id"] == job["id"]
    finally:
        app.close()


def test_a_failing_script_fails_the_job_and_keeps_its_console(tmp_path):
    app = App(tmp_path / "app", register_features=False, register_live=False)
    try:
        script_tools.register(app)
        params = store(tmp_path)
        script = make(tmp_path, "boom.py", "print('before')\nraise SystemExit(3)\n")
        added = app.call("script_tools.add", params | {"path": str(script)})
        job = app.call("script_tools.run", params | {"id": added["id"]})
        record = wait(app, job["id"])
        assert record["status"] == "failed" and "3" in str(record["error"])
        console = app.call("script_tools.console", {"job_id": job["id"]})
        assert "before" in console["output"] and console["status"] == "failed"
    finally:
        app.close()


def test_console_reports_an_unknown_job(tmp_path):
    app = App(tmp_path / "app", register_features=False, register_live=False)
    try:
        script_tools.register(app)
        with pytest.raises(ValueError, match="任务不存在"):
            app.call("script_tools.console", {"job_id": "missing"})
    finally:
        app.close()


def test_run_refuses_a_deleted_script_before_queuing(tmp_path):
    app = App(tmp_path / "app", register_features=False, register_live=False)
    try:
        script_tools.register(app)
        params = store(tmp_path)
        script = make(tmp_path, "gone.py")
        added = app.call("script_tools.add", params | {"path": str(script)})
        script.unlink()
        with pytest.raises(ValueError, match="找不到脚本文件"):
            app.call("script_tools.run", params | {"id": added["id"]})
    finally:
        app.close()


def test_registers_the_public_methods(tmp_path):
    class FakeJobs:
        def __init__(self):
            self.runners = {}

        def register(self, name, runner):
            self.runners[name] = runner

    class FakeApp:
        def __init__(self):
            self.handlers, self.jobs = {}, FakeJobs()

        def register(self, name, handler):
            assert name not in self.handlers
            self.handlers[name] = handler

    app = FakeApp()
    script_tools.register(app)
    assert sorted(app.handlers) == [
        "script_tools.add", "script_tools.console", "script_tools.list",
        "script_tools.remove", "script_tools.reset", "script_tools.run", "script_tools.update",
    ]
    assert list(app.jobs.runners) == [script_tools.TOOL]
    assert [entry["builtin_asset"] for entry in app.handlers["script_tools.list"](store(tmp_path))["entries"]] == ["dsh-mcp-sync.py", "dsh-skill-sync.py"]


def run_asset(asset, *args, timeout=90):
    return subprocess.run([sys.executable, str(asset), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def library_with(tmp_path, valid="good-skill", broken="Bad Name"):
    library = tmp_path / "library"
    for name in (valid, broken):
        (library / name).mkdir(parents=True)
        (library / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 说明\n---\n正文\n", encoding="utf-8")
    (library / "notes.txt").write_text("不是技能", encoding="utf-8")
    return library


def test_skill_script_reports_the_library_and_writes_the_provider_row(tmp_path):
    library = library_with(tmp_path)
    target = tmp_path / "cordis.patch.yml"
    target.write_text("# user patch\n[]\n", encoding="utf-8")
    preview = run_asset(SKILL_ASSET, "--library", str(library), "--target", str(target), "--dry-run")
    assert preview.returncode == 0
    assert "2 个技能，其中 1 个可用" in preview.stdout
    assert "name 必须是 kebab-case" in preview.stdout
    assert "1 项不是技能目录" in preview.stdout
    assert target.read_text(encoding="utf-8") == "# user patch\n[]\n"

    first = run_asset(SKILL_ASSET, "--library", str(library), "--target", str(target))
    assert first.returncode == 0 and "已写入提供方 cc-switch" in first.stdout
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# user patch\n[]\n\n# >>> dsh-skill-sync\n") and text.rstrip().endswith("# <<< dsh-skill-sync")
    assert 'providerName: "cc-switch"' in text and "includeDefaultRoots: false" in text
    assert f'- "{str(library).replace(chr(92), "/")}"' in text
    again = run_asset(SKILL_ASSET, "--library", str(library), "--target", str(target))
    assert "已是最新，无需写入。" in again.stdout


def test_skill_script_refuses_a_duplicate_row_outside_the_markers(tmp_path):
    library = library_with(tmp_path)
    target = tmp_path / "cordis.patch.yml"
    target.write_text('[]\n\n- insert:\n    - id: skill-ccswitch\n      name: "x"\n', encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    result = run_asset(SKILL_ASSET, "--library", str(library), "--target", str(target))
    assert result.returncode != 0 and "挂载两次" in (result.stdout + result.stderr)
    assert target.read_text(encoding="utf-8") == before


def test_skill_script_reports_a_missing_library(tmp_path):
    result = run_asset(SKILL_ASSET, "--library", str(tmp_path / "absent"), "--target", str(tmp_path / "patch.yml"))
    assert result.returncode != 0 and "找不到技能库" in (result.stdout + result.stderr)
