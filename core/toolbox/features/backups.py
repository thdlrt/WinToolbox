"""Portable, verified data snapshots. Secrets only travel inside encrypted backups."""
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from ._common import atomic_write, child_path, extract_zip, write_json

MAGIC = b"WTBXENC1"
ALLOWED = {"settings.json", "engine.sqlite3", "knowledge", "plugins", "models", "runtimes", "media", "library", "live", "sessions", "jobs", "artifacts", "file-operations", "practice", "expenses", "fnconnect", "project-memory"}
MEDIA_EXTENSIONS = {".wav", ".mp3", ".mp4", ".mkv", ".mov", ".m4a", ".flac", ".ogg", ".webm", ".avi", ".aac", ".opus"}


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def key_for(password, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600000).derive(password.encode("utf-8"))


def encrypt(source, destination, password, job):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    cipher = Cipher(algorithms.AES(key_for(password, salt)), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    with Path(source).open("rb") as src, Path(destination).open("xb") as dest:
        dest.write(header)
        while block := src.read(1024 * 1024):
            job.check_cancelled()
            dest.write(cipher.update(block))
        dest.write(cipher.finalize())
        dest.write(cipher.tag)


def decrypt(source, destination, password, job):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    size = Path(source).stat().st_size
    if size < 52:
        raise ValueError("加密备份不完整")
    with Path(source).open("rb") as src, Path(destination).open("xb") as dest:
        header = src.read(36)
        src.seek(-16, os.SEEK_END)
        tag = src.read(16)
        src.seek(36)
        cipher = Cipher(algorithms.AES(key_for(password, header[8:24])), modes.GCM(header[24:36], tag)).decryptor()
        cipher.authenticate_additional_data(header)
        remaining = size - 52
        try:
            while remaining:
                job.check_cancelled()
                block = src.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("加密备份不完整")
                remaining -= len(block)
                dest.write(cipher.update(block))
            dest.write(cipher.finalize())
        except InvalidTag as exc:
            raise ValueError("备份密码错误或文件已损坏") from exc


def snapshot_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(source))) as src, contextlib.closing(sqlite3.connect(str(target))) as dest:
        src.backup(dest)
        check = dest.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"数据库快照校验失败：{source.name}")


def rebase_value(value, old_root, new_root):
    if isinstance(value, dict):
        return {k: rebase_value(v, old_root, new_root) for k, v in value.items()}
    if isinstance(value, list):
        return [rebase_value(v, old_root, new_root) for v in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        previous = str(old_root).replace("\\", "/").rstrip("/")
        if normalized.casefold() == previous.casefold() or normalized.casefold().startswith(previous.casefold() + "/"):
            return str(Path(new_root) / normalized[len(previous):].lstrip("/"))
    return value


def rebase_snapshot(directory, old_root, new_root, extra_mappings=None):
    pairs = sorted([(old_root, new_root), *(extra_mappings or [])], key=lambda pair: len(str(pair[0])), reverse=True)

    def transform(value):
        if isinstance(value, dict):
            return {key: transform(item) for key, item in value.items()}
        if isinstance(value, list):
            return [transform(item) for item in value]
        for previous, current in pairs:
            changed = rebase_value(value, previous, current)
            if changed != value:
                return changed
        return value

    for path in directory.rglob("*"):
        if path.is_file() and path.name == "pyvenv.cfg":
            text = path.read_text(encoding="utf-8")
            lines = []
            for line in text.splitlines():
                name, separator, value = line.partition("=")
                if separator and name.strip() in ("home", "executable", "base-executable", "base-prefix", "base-exec-prefix"):
                    line = name + "= " + str(transform(value.strip()))
                lines.append(line)
            atomic_write(path, "\n".join(lines) + "\n")
        elif path.is_file() and path.suffix == ".json":
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                continue
            write_json(path, transform(value))
        elif path.is_file() and path.suffix in (".sqlite", ".sqlite3", ".db"):
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份中的数据库已损坏")
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    columns = db.execute(f"PRAGMA table_info({quoted_table})").fetchall()
                    for column in columns:
                        if column[2].upper() != "TEXT":
                            continue
                        quoted_column = '"' + column[1].replace('"', '""') + '"'
                        for rowid, text in db.execute(f"SELECT rowid,{quoted_column} FROM {quoted_table}").fetchall():
                            if not isinstance(text, str):
                                continue
                            try:
                                parsed = json.loads(text)
                            except ValueError:
                                changed = transform(text)
                            else:
                                changed = json.dumps(transform(parsed), ensure_ascii=False)
                                if table == "jobs" and isinstance(parsed, dict) and parsed.get("status") in ("running", "queued", "cancelling"):
                                    restored = json.loads(changed)
                                    restored.update(status="interrupted", message="任务来自备份，可检查输入后重试")
                                    changed = json.dumps(restored, ensure_ascii=False)
                            if changed != text:
                                db.execute(f"UPDATE {quoted_table} SET {quoted_column}=? WHERE rowid=?", (changed, rowid))


def register(app):
    # Passwords are never placed in persistent job parameters or emitted events.
    credentials = {}
    credential_lock = threading.Lock()

    def password_for(job):
        internal = getattr(job, '_backup_password', None)
        if internal is not None:
            return internal
        token = job.params.get("credential_token")
        if not token:
            return ""
        with credential_lock:
            if token not in credentials:
                raise ValueError("密码未保存在任务记录中，请重新发起备份操作并输入密码")
            return credentials[token]

    def export_job(job):
        params = job.params
        password = password_for(job)
        include_secrets = bool(params.get("include_secrets", False))
        if include_secrets and len(password) < 8:
            raise ValueError("包含 API 密钥时，备份密码至少需要 8 个字符")
        if password and len(password) < 8:
            raise ValueError("备份密码至少需要 8 个字符")
        destination = Path(params["path"]).expanduser().resolve()
        if destination.exists():
            raise ValueError("备份目标已存在，请选择新的文件名")
        if destination.is_relative_to(app.data_dir.resolve()):
            raise ValueError("请将备份保存到应用数据目录之外")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wintoolbox-backup-", dir=destination.parent) as temporary:
            temporary = Path(temporary)
            snapshot = temporary / "snapshot"
            snapshot.mkdir()
            files = [p for p in app.data_dir.rglob("*") if p.is_file() and not p.is_symlink()]
            selected = []
            for path in files:
                relative = path.relative_to(app.data_dir)
                if relative.parts[0] not in ALLOWED or any(part.startswith(".") for part in relative.parts[:-1]):
                    continue
                if path.name.endswith(("-wal", "-shm", ".tmp")) or path.name == "secrets.json":
                    continue
                if not params.get("include_models", False) and relative.parts[0] in ("models", "runtimes"):
                    continue
                if not params.get("include_media", True) and path.suffix.lower() in MEDIA_EXTENSIONS:
                    continue
                selected.append((path, relative))
            # A user-selected media library may live on another drive. Bring its
            # content into the portable snapshot and record its path mapping.
            mappings = []
            warnings = []
            external_library = app.settings.get().get("preferences", {}).get("library_path")
            if external_library:
                external_library = Path(external_library).expanduser().resolve()
                if external_library.is_dir() and not external_library.is_relative_to(app.data_dir.resolve()):
                    if destination.is_relative_to(external_library):
                        raise ValueError("请将备份保存到媒体库目录之外")
                    relative_root = Path("library") / ("external-" + hashlib.sha256(str(external_library).encode()).hexdigest()[:12])
                    mappings.append({"source": str(external_library), "destination": relative_root.as_posix()})
                    for path in external_library.rglob("*"):
                        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(external_library):
                            continue
                        if not params.get("include_media", True) and path.suffix.lower() in MEDIA_EXTENSIONS:
                            continue
                        selected.append((path, relative_root / path.relative_to(external_library)))
            included_sources = {str(path.resolve()).casefold() for path, _ in selected}
            if hasattr(app.jobs, "list"):
                for record in app.jobs.list():
                    for artifact in record.get("artifacts", []):
                        if artifact.get("kind") == "backup" or not artifact.get("path"):
                            continue
                        path = Path(artifact["path"]).resolve()
                        if str(path).casefold() in included_sources or path.is_relative_to(app.data_dir.resolve()):
                            continue
                        if external_library and path.is_relative_to(external_library):
                            continue
                        if not path.is_file():
                            warnings.append("任务输出已不在原位置，未包含：" + str(path))
                            continue
                        if not params.get("include_media", True) and path.suffix.lower() in MEDIA_EXTENSIONS:
                            continue
                        if not params.get("include_models", False) and artifact.get("kind") in ("model", "model-package", "runtime", "runtime-package"):
                            continue
                        relative = Path("library") / "external-artifacts" / hashlib.sha256(str(path).encode()).hexdigest()[:12] / path.name
                        selected.append((path, relative))
                        included_sources.add(str(path).casefold())
                        mappings.append({"source": str(path), "destination": relative.as_posix()})
            manifest = {"format": "WinToolbox backup", "version": 1, "created_at": time.time(), "data_dir": str(app.data_dir.resolve()),
                        "include_media": params.get("include_media", True), "include_models": params.get("include_models", False),
                        "include_secrets": include_secrets, "path_mappings": mappings, "warnings": warnings, "files": {}}
            # Explicit empty roots make a snapshot restore propagate deletions.
            # Omitted optional models remain outside the snapshot's scope.
            manifest['directories'] = sorted(name for name in ALLOWED - {'settings.json', 'engine.sqlite3'}
                                             if params.get('include_models', False) or name not in ('models', 'runtimes'))
            for name in manifest['directories']:
                (snapshot / name).mkdir(exist_ok=True)
            for i, (path, relative) in enumerate(selected):
                job.check_cancelled()
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix in (".sqlite", ".sqlite3", ".db"):
                    snapshot_database(path, target)
                else:
                    before = path.stat()
                    shutil.copy2(path, target)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError(f"备份期间文件发生变化，请等待相关任务完成后重试：{path.name}")
                manifest["files"][relative.as_posix()] = {"sha256": file_hash(target), "size": target.stat().st_size}
                job.progress((i + 1) / max(1, len(selected)) * 70, "正在备份 " + relative.name)
            if include_secrets:
                write_json(snapshot / "portable-secrets.json", app.settings.export_secrets())
                secret_path = snapshot / "portable-secrets.json"
                manifest["files"]["portable-secrets.json"] = {"sha256": file_hash(secret_path), "size": secret_path.stat().st_size}
            archive_path = temporary / "payload.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
                archive.writestr("backup-manifest.json", json.dumps(manifest, ensure_ascii=False))
                for name in manifest['directories']:
                    archive.writestr(name + '/', b'')
                for name in manifest["files"]:
                    job.check_cancelled()
                    archive.write(snapshot / name, name)
            output = temporary / "complete.wtbak"
            if password:
                job.progress(80, "正在加密备份")
                encrypt(archive_path, output, password, job)
            else:
                os.replace(archive_path, output)
            job.check_cancelled()
            # Exclusive final publication avoids silently replacing an existing backup.
            created = False
            try:
                with output.open("rb") as src, destination.open("xb") as dest:
                    created = True
                    while block := src.read(1024 * 1024):
                        job.check_cancelled()
                        dest.write(block)
                    dest.flush()
                    os.fsync(dest.fileno())
            except BaseException:
                if created:
                    destination.unlink(missing_ok=True)
                raise
        job.artifact(destination, "backup", "工具箱备份")
        return {"path": str(destination), "encrypted": bool(password), "files": len(manifest["files"]), "include_secrets": include_secrets,
                "warnings": manifest["warnings"]}

    def import_job(job):
        source = Path(job.params["path"]).expanduser().resolve()
        password = password_for(job)
        if not source.is_file():
            raise ValueError("备份文件不存在")
        with tempfile.TemporaryDirectory(prefix=".toolbox-restore-", dir=app.data_dir.parent) as temporary:
            temporary = Path(temporary)
            staged = temporary / "staged"
            staged.mkdir()
            with source.open("rb") as header_stream:
                encrypted = header_stream.read(8) == MAGIC
            archive_path = source
            if encrypted:
                if not password:
                    raise ValueError("此备份需要密码")
                archive_path = temporary / "decrypted.zip"
                decrypt(source, archive_path, password, job)
            with zipfile.ZipFile(archive_path) as archive:
                extract_zip(archive, staged, max_bytes=1024**4, max_files=200000)
            manifest_path = staged / "backup-manifest.json"
            if not manifest_path.is_file():
                raise ValueError("不是 WinToolbox 备份文件")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "WinToolbox backup" or manifest.get("version") != 1:
                raise ValueError("不支持的备份版本")
            expected = manifest.get("files")
            if not isinstance(expected, dict) or not isinstance(manifest.get("data_dir"), str) or not manifest["data_dir"]:
                raise ValueError("备份清单无效")
            directories = manifest.get('directories', [])
            if not isinstance(directories, list) or len(directories) > len(ALLOWED):
                raise ValueError('备份目录范围无效')
            for name in directories:
                if (not isinstance(name, str) or name not in ALLOWED - {'settings.json', 'engine.sqlite3'}
                        or not manifest.get('include_models', False) and name in ('models', 'runtimes')):
                    raise ValueError('备份包含不允许恢复的目录')
                (staged / name).mkdir(exist_ok=True)
            actual = {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file() and p != manifest_path}
            if actual != set(expected):
                raise ValueError("备份文件与清单不一致")
            file_roots = {Path(name).parts[0] for name in expected}
            for entry in staged.iterdir():
                if entry.is_dir() and (entry.name not in ALLOWED - {'settings.json', 'engine.sqlite3'}
                                       or entry.name not in directories and entry.name not in file_roots):
                    raise ValueError('备份包含未声明或不允许恢复的目录')
            for i, (name, metadata) in enumerate(expected.items()):
                job.check_cancelled()
                path = child_path(staged, name)
                if name != "portable-secrets.json" and Path(name).parts[0] not in ALLOWED:
                    raise ValueError("备份包含不允许恢复的路径")
                if not manifest.get('include_models', False) and Path(name).parts[0] in ('models', 'runtimes'):
                    raise ValueError('备份模型文件超出声明的恢复范围')
                if path.stat().st_size != metadata["size"] or file_hash(path) != metadata["sha256"]:
                    raise ValueError("备份内容校验失败：" + name)
                job.progress((i + 1) / max(1, len(expected)) * 60, "正在校验 " + path.name)
            secrets_path = staged / "portable-secrets.json"
            portable_secrets = None
            if secrets_path.exists():
                if not encrypted or not manifest.get("include_secrets"):
                    raise ValueError("拒绝从未加密备份恢复 API 密钥")
                portable_secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
                if not isinstance(portable_secrets, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in portable_secrets.items()):
                    raise ValueError("备份密钥格式无效")
                secrets_path.unlink()
            manifest_path.unlink()
            mappings = manifest.get("path_mappings", [])
            if not isinstance(mappings, list) or len(mappings) > 20000:
                raise ValueError("备份中的外部路径映射无效")
            validated_mappings = []
            for mapping in mappings:
                if not isinstance(mapping, dict) or not isinstance(mapping.get("source"), str) or not mapping["source"]:
                    raise ValueError("备份中的外部路径映射无效")
                mapped_path = child_path(app.data_dir, mapping.get("destination", ""))
                if not mapped_path.is_relative_to(app.data_dir / "library"):
                    raise ValueError("外部媒体库只能迁移到应用媒体目录")
                validated_mappings.append((mapping["source"], mapped_path))
            rebase_snapshot(staged, manifest["data_dir"], app.data_dir, validated_mappings)
            if not manifest.get("include_media", True):
                # Replacing a sessions/library folder must not delete media
                # deliberately omitted by the backup options.
                for current in app.data_dir.rglob("*"):
                    if not current.is_file() or current.is_symlink() or current.suffix.lower() not in MEDIA_EXTENSIONS:
                        continue
                    relative = current.relative_to(app.data_dir)
                    if (staged / relative.parts[0]).is_dir():
                        preserved = staged / relative
                        if not preserved.exists():
                            preserved.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(current, preserved)
            job.check_cancelled()
            before_commit = getattr(job, '_before_backup_commit', None)
            if before_commit:
                before_commit(manifest)
                job.check_cancelled()
            rollback = temporary / "rollback"
            rollback.mkdir()
            jobs_lock = getattr(app.jobs, "lock", contextlib.nullcontext())
            storage_lock = getattr(getattr(app, "storage", None), "lock", contextlib.nullcontext())
            replaced = []
            moved_old = []
            secrets_modified = False
            restore_prepared = False
            data_lock = getattr(app, 'data_lock', contextlib.nullcontext())
            with data_lock, jobs_lock, storage_lock:
                active = getattr(app.jobs, "active", {})
                if any(identifier != job.id for identifier in active):
                    raise ValueError("请等待或取消其他运行中任务后再恢复备份")
                app.maintenance = True
                try:
                    if hasattr(app, "before_restore"):
                        app.before_restore(job.id)
                    restore_prepared = True
                    old_secrets = app.data_dir / "secrets.json"
                    if old_secrets.exists():
                        shutil.copy2(old_secrets, rollback / "secrets.json")
                    for path in list(staged.iterdir()):
                        destination = app.data_dir / path.name
                        if destination.exists():
                            os.replace(destination, rollback / path.name)
                            moved_old.append(path.name)
                        os.replace(path, destination)
                        replaced.append(path.name)
                    app.settings.reload()
                    if portable_secrets is not None:
                        secrets_modified = True
                        app.settings.import_secrets(portable_secrets)
                    if hasattr(app, "after_restore"):
                        app.after_restore()
                    # The restored engine snapshot predates this import job.
                    # Preserve its live record before releasing the storage lock
                    # so jobs.get never transiently reports it as missing.
                    if hasattr(job, "save"):
                        job.save()
                    if hasattr(job, 'mark_committed'):
                        job.mark_committed()
                except BaseException:
                    for name in reversed(replaced):
                        target = app.data_dir / name
                        if target.is_dir():
                            shutil.rmtree(target)
                        elif target.exists():
                            target.unlink()
                    for name in moved_old:
                        os.replace(rollback / name, app.data_dir / name)
                    if (rollback / "secrets.json").exists():
                        os.replace(rollback / "secrets.json", app.data_dir / "secrets.json")
                    elif secrets_modified and (app.data_dir / "secrets.json").exists():
                        (app.data_dir / "secrets.json").unlink()
                    app.settings.reload()
                    if restore_prepared and hasattr(app, 'after_restore'):
                        with contextlib.suppress(Exception):
                            app.after_restore()
                    raise
                finally:
                    app.maintenance = False
        app.emit("app.restored", restart_recommended=True)
        return {"ok": True, "restart_recommended": True, "message": "备份已恢复。请重启工具箱以重新载入全部运行环境。未包含的模型和录音目录保持现状。"}

    def submit(tool, runner, params):
        safe_params = dict(params)
        password = safe_params.pop("password", "")
        if not isinstance(password, str):
            raise ValueError("密码应为文本")
        if password:
            token = uuid.uuid4().hex
            with credential_lock:
                credentials[token] = password
            safe_params["credential_token"] = token
        return app.jobs.submit(tool, safe_params, runner)

    app.jobs.register("backup-export", export_job)
    app.jobs.register("backup-import", import_job)
    app.register("backups.export", lambda params: submit("backup-export", export_job, params))
    app.register("backups.import", lambda params: submit("backup-import", import_job, params))

    def run_internal(operation, job, params, *, password='', before_commit=None):
        """Reuse verified backup code without nesting jobs or persisting passwords."""
        class InternalJob:
            def __init__(self):
                self.params = dict(params)
                self._backup_password = password
                self._before_backup_commit = before_commit

            def __getattr__(self, name):
                return getattr(job, name)
        if operation not in ('export', 'import'):
            raise ValueError('未知备份操作')
        return (export_job if operation == 'export' else import_job)(InternalJob())

    app.backup_internal = run_internal
