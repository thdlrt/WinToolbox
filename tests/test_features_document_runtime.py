"""Offline document-runtime routing and portable Python metadata fixtures."""
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from toolbox.app import App
from toolbox.features.backups import rebase_snapshot
from toolbox.features.knowledge import office_executable
from toolbox.features._common import write_json
from toolbox.jobs import Job


def await_job(app, job, expected="completed"):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = app.jobs.get(job["id"])
        if result["status"] not in ("queued", "running", "cancelling") and job["id"] not in app.jobs.active:
            assert result["status"] == expected, result
            return result
        time.sleep(0.02)
    raise AssertionError("fixture job timed out")


@pytest.fixture
def pair(tmp_path, monkeypatch):
    source = App(tmp_path / "source-app", register_live=False)
    target = App(tmp_path / "target-app", register_live=False)
    runtime = source.data_dir / "runtimes" / "libreoffice"
    (runtime / "program").mkdir(parents=True)
    (runtime / "program" / "soffice.exe").write_bytes(b"MZ-synthetic-runtime-fixture")
    (runtime / "program" / "version.ini").write_text("version=26.8.0", encoding="utf-8")
    write_json(runtime / "installed.json", {"version": "26.8.0", "sha256": "0" * 64, "exe": str(runtime / "program" / "soffice.exe")})
    commands = []
    def process_fixture(job, command, **kwargs):
        commands.append(command)
        assert Path(command[0]).name == "soffice.exe"
        assert "--headless" in command and "--version" in command
        return "LibreOffice 26.8.0 fixture only"
    monkeypatch.setattr(Job, "run_process", process_fixture)
    try:
        yield source, target, commands
    finally:
        source.close()
        target.close()
        source.jobs.pool.shutdown(wait=True)
        target.jobs.pool.shutdown(wait=True)


def test_document_runtime_offline_roundtrip_routes_model_center(pair, tmp_path):
    source, target, commands = pair
    package = tmp_path / "libreoffice.zip"
    export = await_job(source, source.call("models.export", {"model_id": "libreoffice", "path": str(package)}))
    assert export["result"]["files"] == 3
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("package.json"))
        assert manifest["format"] == "wintoolbox-runtime" and manifest["engine"] == "document"
        assert manifest["model_id"] == "libreoffice"
    imported = await_job(target, target.call("models.import", {"path": str(package)}))
    assert imported["result"]["installed"]
    path = Path(target.settings.get()["preferences"]["libreoffice_path"])
    assert path.is_file() and path.is_relative_to(target.data_dir)
    assert any(item["id"] == "libreoffice" and item["installed"] for item in target.call("models.list")["models"])
    assert commands
    # Simulate an old absolute preference after a portable directory move.
    relocated = path.parent.parent / "stage-portable" / "program"
    relocated.mkdir(parents=True)
    new_path = relocated / "soffice.exe"
    os.replace(path, new_path)
    target.settings.update({"preferences": {"libreoffice_path": str(tmp_path / "old-location" / "soffice.exe")}})
    assert office_executable(target) == new_path


def test_document_runtime_tampered_archive_never_installs(pair, tmp_path):
    source, target, commands = pair
    package = tmp_path / "source.zip"
    await_job(source, source.call("models.export", {"model_id": "libreoffice", "path": str(package)}))
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(package) as original, zipfile.ZipFile(tampered, "w") as archive:
        for name in original.namelist():
            archive.writestr(name, b"changed" if name.endswith("soffice.exe") else original.read(name))
    result = await_job(target, target.call("models.import", {"path": str(tampered)}), expected="failed")
    assert "校验失败" in result["error"]
    assert not (target.data_dir / "runtimes" / "libreoffice").exists()
    assert not commands


def test_document_runtime_traversal_rejected(pair, tmp_path):
    _, target, _ = pair
    package = tmp_path / "traversal.zip"
    manifest = {"format": "wintoolbox-runtime", "version": 1, "model_id": "libreoffice", "engine": "document", "platform": sys.platform,
                "files": {"runtime/../../escape.exe": "0" * 64}, "entrypoint": "runtime/../../escape.exe"}
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("package.json", json.dumps(manifest))
        archive.writestr("runtime/../../escape.exe", "bad")
    result = await_job(target, target.call("models.import", {"path": str(package)}), expected="failed")
    assert "越界" in result["error"]
    assert not (target.data_dir / "escape.exe").exists()


def test_document_runtime_export_retains_other_model_routing(pair, tmp_path):
    source, _, _ = pair
    result = await_job(source, source.call("models.export", {"model_id": "nonexistent-model", "path": str(tmp_path / "unused.zip")}), expected="failed")
    assert "未知本地模型" in result["error"]


def test_backup_rebases_venv_cfg_without_changing_options(tmp_path):
    old = tmp_path / "old-machine"
    new = tmp_path / "new-machine"
    stage = tmp_path / "stage"
    stage.mkdir()
    config = stage / "pyvenv.cfg"
    config.write_text(f"home = {old / 'runtimes/python'}\nexecutable = {old / 'runtimes/python/python.exe'}\ninclude-system-site-packages = false\nversion = 3.12.12\n", encoding="utf-8")
    rebase_snapshot(stage, old, new)
    text = config.read_text(encoding="utf-8")
    assert str(new / "runtimes/python") in text and str(old) not in text
    assert "include-system-site-packages = false" in text and "version = 3.12.12" in text
