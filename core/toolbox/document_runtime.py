"""Install a private LibreOffice runtime without registering a system application."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
import httpx

from .features._common import atomic_write, child_path, extract_zip, write_json

VERSION = "26.8.0"
FILENAME = f"LibreOffice_{VERSION}_Win_x86-64.msi"
URL = f"https://download.documentfoundation.org/libreoffice/stable/{VERSION}/win/x86_64/{FILENAME}"


def register(app):
    existing_list = app.handlers["models.list"]
    existing_install = app.handlers["models.install"]
    existing_remove = app.handlers["models.remove"]
    existing_export = app.jobs.runners["models.export"]
    existing_import = app.jobs.runners["models.import"]
    root = app.data_dir / "runtimes" / "libreoffice"
    install_lock = app.models.install_lock

    def executable():
        return next(root.glob("**/program/soffice.exe"), None) if root.exists() else None

    def listing(params):
        result = existing_list(params)
        result["models"].append({"id": "libreoffice", "name": "LibreOffice 文档转换", "engine": "document", "installed": executable() is not None, "size_hint": "约 400 MB 下载 / 1 GB 磁盘", "description": "Office 页面渲染，用于 PPT 图表和 Word 页码引用；不修改系统 Office"})
        return result

    def install_job(job):
        root.mkdir(parents=True, exist_ok=True)
        msi = job.work_dir / FILENAME
        job.progress(2, "读取官方 LibreOffice 运行包校验信息")
        with httpx.Client(follow_redirects=True, timeout=60) as client:
            checksum_reply = client.get(URL + ".sha256")
            checksum_reply.raise_for_status()
            expected = checksum_reply.text.strip().split()[0].lower()
            if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                raise RuntimeError("官方运行包 SHA256 格式不正确，已停止安装")
            sha = hashlib.sha256()
            with client.stream("GET", URL) as response:
                response.raise_for_status()
                size = int(response.headers.get("content-length", 0))
                received = 0
                reported = 0
                with msi.open("wb") as output:
                    for block in response.iter_bytes(1024 * 1024):
                        job.check_cancelled()
                        output.write(block)
                        sha.update(block)
                        received += len(block)
                        percent = int(received / size * 70) if size else 20
                        if percent != reported:
                            job.progress(percent + 3, f"下载文档转换运行包 {received // 1048576} MB")
                            reported = percent
            if sha.hexdigest() != expected:
                raise RuntimeError("运行包 SHA256 校验失败，未执行安装")
        job.progress(76, "正在提取独立文档运行环境")
        stage = root / ("stage-" + job.id)
        stage.mkdir()
        log = job.work_dir / "libreoffice-extract.log"
        job.run_process(["msiexec.exe", "/a", str(msi), "/qn", "TARGETDIR=" + str(stage), "/l*v", str(log)], timeout=300)
        found = next(stage.glob("**/program/soffice.exe"), None)
        if not found:
            raise RuntimeError("文档运行环境提取失败，请打开任务日志查看原因")
        settings = app.settings.get()
        prefs = settings.get("preferences", {})
        prefs["libreoffice_path"] = str(found)
        app.settings.update({"preferences": prefs})
        (root / "installed.json").write_text(json.dumps({"version": VERSION, "sha256": expected, "exe": str(found)}, indent=2), encoding="utf-8")
        job.progress(100, "Office 页面渲染已就绪")
        job.artifact(root / "installed.json", "runtime", "文档转换运行环境")
        return {"path": str(found)}

    def install(params):
        if params.get("model_id") == "libreoffice":
            if executable():
                raise ValueError("文档运行环境已安装")
            return app.jobs.submit("documents.runtime.install", params, install_job)
        return existing_install(params)

    def remove(params):
        if params.get("model_id") != "libreoffice":
            return existing_remove(params)
        if any(j["status"] in ("running", "queued") for j in app.jobs.list()):
            raise ValueError("请先等待正在运行的任务结束")
        if root.exists():
            resolved = root.resolve()
            if not resolved.is_relative_to(app.data_dir.resolve()) or root.is_symlink():
                raise ValueError("运行目录不在数据目录内")
            shutil.rmtree(resolved)
        app.settings.update({"preferences": {"libreoffice_path": None}})
        return {"ok": True}

    def hash_file(path, job):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            while block := stream.read(1024 * 1024):
                job.check_cancelled()
                digest.update(block)
        return digest.hexdigest()

    def export_runtime(job):
        if job.params.get("model_id") != "libreoffice":
            return existing_export(job)
        with install_lock:
            found = executable()
            marker = root / "installed.json"
            if not found or not marker.is_file():
                raise ValueError("请先在模型中心安装 LibreOffice 文档运行环境")
            installed = json.loads(marker.read_text(encoding="utf-8"))
            destination = Path(job.params["path"]).expanduser().resolve()
            if destination.exists():
                raise ValueError("导出文件已存在，请选择新的文件名")
            if destination.is_relative_to(root.resolve()):
                raise ValueError("请将离线包保存到文档运行目录之外")
            destination.parent.mkdir(parents=True, exist_ok=True)
            files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
                     and path.resolve().is_relative_to(root.resolve())]
            total = sum(path.stat().st_size for path in files)
            completed = 0
            reported = -1
            checksums = {}
            with tempfile.TemporaryDirectory(prefix=".libreoffice-export-", dir=destination.parent) as temporary:
                output = Path(temporary) / "complete.zip"
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                    for path in files:
                        job.check_cancelled()
                        name = "runtime/" + path.relative_to(root).as_posix()
                        sha = hashlib.sha256()
                        before = path.stat()
                        with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as dest:
                            while block := source.read(1024 * 1024):
                                job.check_cancelled()
                                sha.update(block)
                                dest.write(block)
                                completed += len(block)
                                percent = int(completed / max(1, total) * 95)
                                if percent != reported:
                                    job.progress(percent, "导出文档运行包：" + path.name)
                                    reported = percent
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError("运行环境在导出期间发生变化，请重试")
                        checksums[name] = sha.hexdigest()
                    manifest = {"format": "wintoolbox-runtime", "version": 1, "model_id": "libreoffice", "engine": "document",
                                "platform": sys.platform, "runtime_version": installed.get("version", VERSION),
                                "entrypoint": "runtime/" + found.relative_to(root).as_posix(),
                                "upstream_sha256": installed.get("sha256"), "files": checksums}
                    archive.writestr("package.json", json.dumps(manifest, ensure_ascii=False))
                # Publish without overwriting a concurrently created file.
                created = False
                try:
                    with output.open("rb") as source, destination.open("xb") as dest:
                        created = True
                        while block := source.read(1024 * 1024):
                            job.check_cancelled()
                            dest.write(block)
                        dest.flush()
                        os.fsync(dest.fileno())
                except BaseException:
                    if created:
                        destination.unlink(missing_ok=True)
                    raise
            job.artifact(destination, "runtime-package", "LibreOffice 文档离线包")
            return {"path": str(destination), "model_id": "libreoffice", "files": len(checksums)}

    def import_runtime(job):
        source = Path(job.params["path"]).expanduser().resolve()
        with zipfile.ZipFile(source) as archive:
            manifest = json.loads(archive.read("package.json"))
        if not isinstance(manifest, dict) or manifest.get("model_id") != "libreoffice":
            return existing_import(job)
        if manifest.get("format") != "wintoolbox-runtime" or manifest.get("version") != 1 or manifest.get("engine") != "document" or manifest.get("platform") != sys.platform:
            raise ValueError("文档离线包版本、引擎或平台不兼容")
        declared = manifest.get("files")
        if not isinstance(declared, dict) or not declared or any(not name.startswith("runtime/") for name in declared):
            raise ValueError("文档离线包文件清单无效")
        with install_lock, tempfile.TemporaryDirectory(prefix=".libreoffice-import-", dir=app.data_dir / "runtimes") as temporary:
            staged = Path(temporary)
            if root.exists() and any(root.iterdir()):
                raise ValueError("文档运行目录已存在，请先在模型中心移除现有运行环境")
            job.progress(3, "正在展开文档离线包")
            with zipfile.ZipFile(source) as archive:
                actual = {item.orig_filename for item in archive.infolist() if not item.is_dir() and item.orig_filename != "package.json"}
                if actual != set(declared):
                    raise ValueError("文档离线包文件清单不完整")
                extract_zip(archive, staged, max_bytes=8 * 1024**3, max_files=30000, check_cancel=job.check_cancelled)
            job.progress(45, "校验文档运行环境")
            reported = -1
            for index, (name, expected) in enumerate(declared.items()):
                if hash_file(child_path(staged, name), job) != expected:
                    raise ValueError("文档离线包校验失败：" + name)
                percent = 45 + int((index + 1) / len(declared) * 40)
                if percent != reported:
                    job.progress(percent, "校验运行文件：" + Path(name).name)
                    reported = percent
            found = child_path(staged, manifest.get("entrypoint", ""))
            runtime = staged / "runtime"
            if not found.is_relative_to(runtime) or found.name.lower() != "soffice.exe" or not found.is_file():
                raise ValueError("文档离线包缺少 soffice.exe 入口")
            marker = runtime / "installed.json"
            if not marker.is_file():
                raise ValueError("文档离线包缺少安装信息")
            installed = json.loads(marker.read_text(encoding="utf-8"))
            job.progress(88, "测试导入后的 LibreOffice")
            profile = staged / "test-profile"
            console = found.with_suffix(".com")
            probe = console if console.is_file() else found
            output = job.run_process([str(probe), "-env:UserInstallation=" + profile.as_uri(), "--headless", "--version"], timeout=45)
            if "LibreOffice" not in output:
                raise ValueError("文档运行环境无法启动，未完成导入")
            target_exe = root / found.relative_to(runtime)
            installed.update(version=manifest.get("runtime_version", installed.get("version")), exe=str(target_exe),
                             sha256=manifest.get("upstream_sha256", installed.get("sha256")), imported_at=time.time())
            write_json(marker, installed)
            job.check_cancelled()
            settings_path = app.data_dir / "settings.json"
            previous_settings = settings_path.read_bytes() if settings_path.exists() else None
            if root.exists():
                root.rmdir()
            os.replace(runtime, root)
            try:
                app.settings.update({"preferences": {"libreoffice_path": str(target_exe)}})
            except BaseException:
                os.replace(root, runtime)
                if previous_settings is not None:
                    atomic_write(settings_path, previous_settings)
                elif settings_path.exists():
                    settings_path.unlink()
                app.settings.reload()
                raise
            job.artifact(root / "installed.json", "runtime", "导入的文档运行环境")
            return {"model_id": "libreoffice", "installed": True, "path": str(target_exe), "version": installed["version"]}

    app.jobs.register("documents.runtime.install", install_job)
    app.jobs.register("models.export", export_runtime)
    app.jobs.register("models.import", import_runtime)
    app.handlers.update({"models.list": listing, "models.install": install, "models.remove": remove})
