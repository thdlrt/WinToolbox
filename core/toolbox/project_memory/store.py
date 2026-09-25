"""Offline-first immutable project memory, without third-party dependencies."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id():
    return str(uuid.uuid4())


def state_directory():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "WinToolbox" / "agent"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "wintoolbox-agent"


def machine_device_id():
    folder = state_directory()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "device-id"
    if not path.exists():
        temporary = folder / ("device-id." + _id() + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(_id())
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("设备标识为空，请修复 device-id 文件")
    return value


class MemoryStore:
    def __init__(self, root: Path, device_id=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.device_id = device_id or machine_device_id()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "memory.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 1:
            self.db.close()
            raise ValueError("项目记忆数据库来自较新版本，请升级 WinToolbox")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations(op_id TEXT PRIMARY KEY,entity_id TEXT NOT NULL,entity_type TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS operations_entity ON operations(entity_id);
        CREATE TABLE IF NOT EXISTS replicas(device_id TEXT PRIMARY KEY,replica_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS locations(id TEXT PRIMARY KEY,device_id TEXT NOT NULL,project_id TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subscriptions(device_id TEXT NOT NULL,project_id TEXT NOT NULL,enabled INTEGER NOT NULL,PRIMARY KEY(device_id,project_id));
        PRAGMA user_version=1;
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('library_id',?)", (_id(),))
            self.db.execute("INSERT OR IGNORE INTO replicas VALUES(?,?)", (self.device_id, _id()))
        self.library_id = self.db.execute("SELECT value FROM metadata WHERE key='library_id'").fetchone()[0]
        self.replica_id = self.db.execute("SELECT replica_id FROM replicas WHERE device_id=?", (self.device_id,)).fetchone()[0]
        self._upgrade_device_scope()

    def _upgrade_device_scope(self):
        # Legacy device operations were never exported. Convert their complete DAG
        # deterministically so copied stores publish identical operations and IDs.
        old = [op for op in self._all() if op.get("scope") == "device"]
        if not old:
            return
        folder = self.root.parent / "project-memory-local" / "scope-backups"
        folder.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(folder / ("before-global-" + _id() + ".sqlite3"))
        try:
            self.db.backup(backup)
        finally:
            backup.close()
        with self.lock, self.db:
            for op in old:
                op["scope"] = "global"
                op["data"]["scope"] = "global"
                self.db.execute("UPDATE operations SET payload=? WHERE op_id=?", (_json(op), op["op_id"]))

    def close(self):
        with self.lock:
            self.db.close()

    def reopen(self):
        """Reconnect after restoring the containing portable data snapshot."""
        try:
            self.close()
        except sqlite3.ProgrammingError:
            pass
        self.__init__(self.root, device_id=self.device_id)
        return self.info()

    def info(self):
        return {"library_id": self.library_id, "device_id": self.device_id, "replica_id": self.replica_id,
                "schema_version": 1, "operation_count": self.db.execute("SELECT count(*) FROM operations").fetchone()[0]}

    def join_library(self, library_id):
        if not isinstance(library_id, str) or not library_id.strip() or len(library_id) > 200:
            raise ValueError("无效知识库 ID")
        with self.lock, self.db:
            if library_id != self.library_id and self.db.execute("SELECT 1 FROM operations LIMIT 1").fetchone():
                raise ValueError("当前知识库已有记录，不能切换到另一个知识库；请导出后迁移")
            self.db.execute("UPDATE metadata SET value=? WHERE key='library_id'", (library_id,))
            self.library_id = library_id
        return self.info()

    def _all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM operations ORDER BY op_id")]

    def _versions(self, entity_id):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM operations WHERE entity_id=? ORDER BY op_id", (entity_id,))]

    def _heads(self, versions):
        # Topological processing handles arbitrarily long histories and missing parents.
        by_id = {op["op_id"]: op for op in versions}
        remaining = {key: len(op["parents"]) for key, op in by_id.items()}
        children = {}
        for op in versions:
            for parent in op["parents"]:
                children.setdefault(parent, []).append(op["op_id"])
        ready = [key for key, count in remaining.items() if count == 0]
        complete = set()
        while ready:
            key = ready.pop()
            complete.add(key)
            for child in children.get(key, []):
                remaining[child] -= 1
                if remaining[child] == 0:
                    ready.append(child)
        ancestors = {p for key in complete for p in by_id[key]["parents"]}
        return [by_id[key] for key in sorted(complete - ancestors)]

    def _view(self, entity_id):
        versions = self._versions(entity_id)
        heads = self._heads(versions)
        if not heads:
            raise KeyError(entity_id)
        visible = heads
        if not visible:
            raise KeyError(entity_id)
        chosen = sorted(visible, key=lambda op: op["op_id"])[0]
        by_id = {op["op_id"]: op for op in versions}
        reachable = set()
        pending = [op["op_id"] for op in heads]
        while pending:
            op_id = pending.pop()
            if op_id in reachable:
                continue
            reachable.add(op_id)
            pending.extend(by_id[op_id]["parents"])
        return {**chosen["data"], "id": entity_id, "heads": [op["op_id"] for op in visible],
                "conflict": len(visible) > 1, "versions": visible,
                "pending_count": len(versions) - len(reachable)}

    def _validate_data(self, data, entity_type):
        if not isinstance(data, dict):
            raise ValueError("记录内容必须是对象")
        data = json.loads(_json(data))
        forbidden = {"path", "root", "local_path", "password", "password_dpapi", "credentials", "ssh_key", "private_key", "locations"}
        def check(value):
            if isinstance(value, dict):
                if forbidden.intersection(value):
                    raise ValueError("共享记录不能包含设备路径或凭据字段")
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
        check(data)
        if entity_type == "project":
            if not isinstance(data.get("name"), str) or not data["name"].strip():
                raise ValueError("项目名称不能为空")
            data["scope"] = "project"
        elif entity_type == "entry":
            if data.get("kind") not in ("task", "knowledge"):
                raise ValueError("记录类型必须是 task 或 knowledge")
            if data.get("scope", "project") not in ("project", "global"):
                raise ValueError("无效记录作用域")
            data.setdefault("scope", "project")
            if data["scope"] == "project" and not data.get("project_id"):
                raise ValueError("项目记录需要 project_id")
            if not isinstance(data.get("title"), str) or not data["title"].strip():
                raise ValueError("标题不能为空")
            if not isinstance(data.get("body", ""), str):
                raise ValueError("正文必须是文本")
            data.setdefault("body", "")
            data.setdefault("related_ids", [])
            if not isinstance(data["related_ids"], list) or any(not isinstance(v, str) for v in data["related_ids"]):
                raise ValueError("关联记录必须为 ID 列表")
            if data["kind"] == "knowledge":
                data.pop("status", None)
                data.setdefault("knowledge_type", "经验")
            else:
                data.setdefault("status", "active")
                if data["status"] not in ("active", "blocked", "done"):
                    raise ValueError("任务状态必须是 active、blocked 或 done")
            for attachment in data.get("attachments", []):
                self._validate_hash(attachment.get("hash", ""))
        else:
            raise ValueError("无效实体类型")
        for key in ("heads", "conflict", "versions", "pending_count", "available", "subscribed"):
            data.pop(key, None)
        return data

    def _write(self, entity_id, entity_type, data, parents):
        with self.lock, self.db:
            versions = self._versions(entity_id)
            current = {op["op_id"] for op in self._heads(versions)}
            if versions and parents is None:
                raise ValueError("修改已有记录必须提供 parents 版本，防止覆盖并发修改")
            parents = list(parents or [])
            if set(parents) != current:
                raise ValueError("记录已改变，请重新读取并解决版本冲突")
            if versions and any(op["entity_type"] != entity_type for op in versions):
                raise ValueError("实体类型不能改变")
            data = self._validate_data(data, entity_type)
            if versions and any(v["scope"] != data["scope"] or v["data"].get("project_id") != data.get("project_id") for v in versions):
                raise ValueError("记录作用域和所属项目不可改写；请创建关联记录")
            data["id"] = entity_id
            op = {"library_id": self.library_id, "op_id": _id(), "entity_id": entity_id, "entity_type": entity_type,
                  "parents": parents, "replica_id": self.replica_id, "device_id": self.device_id,
                  "scope": data["scope"], "project_id": data.get("project_id", entity_id if entity_type == "project" else None),
                  "data": data, "created_at": datetime.now(timezone.utc).isoformat()}
            self.db.execute("INSERT INTO operations VALUES(?,?,?,?)", (op["op_id"], entity_id, entity_type, _json(op)))
        return self._view(entity_id)

    def create_project(self, name, project_id=None):
        result = self._write(project_id or _id(), "project", {"name": name}, [])
        self.subscribe(result["id"])
        return next(p for p in self.projects() if p["id"] == result["id"])

    def save_project(self, data, parents=None):
        data = dict(data)
        if parents is None and "heads" in data:
            parents = data["heads"]
        return self._write(data.get("id") or _id(), "project", data, parents)

    def get_project(self, project_id):
        if not any(v["entity_type"] == "project" for v in self._versions(project_id)):
            raise KeyError(project_id)
        return next(p for p in self.projects() if p["id"] == project_id)

    def projects(self):
        result = []
        for row in self.db.execute("SELECT DISTINCT entity_id FROM operations WHERE entity_type='project'"):
            try:
                item = self._view(row[0])
            except KeyError:
                continue
            item["locations"] = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM locations WHERE device_id=? AND project_id=?", (self.device_id, item["id"]))]
            item["available"] = any(loc["kind"] == "local" and Path(loc["path"]).is_dir() for loc in item["locations"])
            sub = self.db.execute("SELECT enabled FROM subscriptions WHERE device_id=? AND project_id=?", (self.device_id, item["id"])).fetchone()
            item["subscribed"] = bool(sub and sub[0])
            result.append(item)
        return sorted(result, key=lambda p: (p["name"].casefold(), p["id"]))

    def subscribe(self, project_id, enabled=True):
        self._view(project_id)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES(?,?,?)", (self.device_id, project_id, int(bool(enabled))))
        return {"project_id": project_id, "subscribed": bool(enabled)}

    def bind_project(self, project_id, path, kind="local", host="", **kwargs):
        self._view(project_id)
        if kind not in ("local", "ssh") or not str(path).strip():
            raise ValueError("无效项目位置")
        if kind == "local":
            root = Path(path).expanduser().resolve()
            if not root.is_dir():
                raise ValueError("项目目录不存在；不会自动创建项目目录")
            candidates = [root / ".agent/toolbox.json", root / ".agents/toolbox.json"]
            context_dir = kwargs.get("context_dir")
            if context_dir not in (None, ".agent", ".agents"):
                raise ValueError("context_dir 只能为 .agent 或 .agents")
            if (root / ".agent").exists() and (root / ".agents").exists() and not any(p.exists() for p in candidates) and context_dir is None:
                raise ValueError("同时存在 .agent 和 .agents，请明确保留的项目记忆目录后重试")
            for candidate in candidates:
                if candidate.exists():
                    linked = json.loads(candidate.read_text(encoding="utf-8"))
                    if linked.get("project_id") != project_id or linked.get("library_id") != self.library_id:
                        raise ValueError("目录已关联其他项目或知识库，请显式迁移")
            manifest = next((p for p in candidates if p.exists()), root / (context_dir or (".agents" if (root / ".agents").exists() and not (root / ".agent").exists() else ".agent")) / "toolbox.json")
            manifest.parent.mkdir(exist_ok=True)
            if not manifest.exists():
                try:
                    with manifest.open("x", encoding="utf-8") as handle:
                        handle.write(_json({"schema_version": 1, "library_id": self.library_id, "project_id": project_id}) + "\n")
                except FileExistsError:
                    raise ValueError("项目关联已变化，请重试")
            path = str(root)
        elif not host or host.startswith("-") or any(c in host for c in "\r\n\x00"):
            raise ValueError("SSH 位置需要有效主机别名")
        location_id = hashlib.sha256(_json([self.device_id, project_id, kind, host, str(path)]).encode()).hexdigest()[:32]
        location = {"id": location_id, "device_id": self.device_id, "project_id": project_id, "kind": kind, "path": str(path), "host": host}
        for key in ("label", "python", "store", "port"):
            if key in kwargs:
                location[key] = kwargs[key]
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO locations VALUES(?,?,?,?)", (location_id, self.device_id, project_id, _json(location)))
        self.subscribe(project_id)
        return location

    def unbind_project(self, location_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM locations WHERE id=? AND device_id=?", (location_id, self.device_id))
        return {"removed": True}

    def save_entry(self, data, parents=None):
        data = dict(data)
        if parents is None and "heads" in data:
            parents = data["heads"]
        return self._write(data.get("id") or _id(), "entry", data, parents)

    def get_entry(self, entry_id):
        result = self._view(entry_id)
        if self._versions(entry_id)[0]["entity_type"] != "entry":
            raise ValueError("该 ID 是项目而非记录")
        return result

    def entries(self, project_id=None, kind=None, include_done=False, query="", scope=None):
        result = []
        for row in self.db.execute("SELECT DISTINCT entity_id FROM operations WHERE entity_type='entry'"):
            try:
                item = self.get_entry(row[0])
            except KeyError:
                continue
            if project_id is not None and item.get("project_id") != project_id:
                continue
            if kind and item["kind"] != kind or scope and item["scope"] != scope:
                continue
            if not include_done and (item.get("status") == "done" or item.get("archived")):
                continue
            if query and query.casefold() not in (item["title"] + "\n" + item["body"]).casefold():
                continue
            result.append(item)
        return sorted(result, key=lambda item: (item["title"].casefold(), item["id"]))

    def operation_ids(self):
        return [op["op_id"] for op in self.export_operations()]

    def export_operations(self, known_ids=None):
        known = set(known_ids or [])
        return [op for op in self._all() if op["op_id"] not in known and op.get("scope") != "device"]

    def ingest_operations(self, operations):
        if not isinstance(operations, list):
            raise ValueError("operations 必须是数组")
        accepted = 0
        with self.lock, self.db:
            for op in operations:
                if not isinstance(op, dict) or op.get("library_id") != self.library_id:
                    raise ValueError("同步记录属于其他知识库")
                expected_keys = {"library_id", "op_id", "entity_id", "entity_type", "parents", "replica_id", "device_id", "scope", "project_id", "data", "created_at"}
                if set(op) != expected_keys:
                    raise ValueError("无效同步记录格式或未知字段")
                for key in ("op_id", "entity_id", "replica_id", "device_id", "created_at"):
                    if not isinstance(op.get(key), str) or not op[key] or len(op[key]) > 500:
                        raise ValueError("无效同步记录字段: " + key)
                if op.get("scope") == "device":
                    raise ValueError("设备私有记录不能跨设备同步")
                if not isinstance(op.get("parents"), list) or any(not isinstance(p, str) or not p for p in op["parents"]) or op["op_id"] in op["parents"] or len(set(op["parents"])) != len(op["parents"]):
                    raise ValueError("无效父版本列表")
                data = self._validate_data(op.get("data"), op.get("entity_type"))
                if data != op["data"] or data.get("id") != op["entity_id"] or op.get("scope") != data["scope"] or op.get("project_id") != data.get("project_id", op["entity_id"] if op["entity_type"] == "project" else None):
                    raise ValueError("同步记录元数据不一致")
                payload = _json(op)
                old = self.db.execute("SELECT payload FROM operations WHERE op_id=?", (op["op_id"],)).fetchone()
                if old:
                    if old[0] != payload:
                        raise ValueError("不可变操作被篡改: " + op["op_id"])
                    continue
                existing = self._versions(op["entity_id"])
                if any(v["scope"] != op["scope"] or v["data"].get("project_id") != data.get("project_id") for v in existing):
                    raise ValueError("同步记录作用域冲突")
                if any(v["entity_type"] != op["entity_type"] for v in existing):
                    raise ValueError("同步实体类型冲突")
                self.db.execute("INSERT INTO operations VALUES(?,?,?,?)", (op["op_id"], op["entity_id"], op["entity_type"], payload))
                accepted += 1
            all_ops = {op["op_id"]: op for op in self._all()}
            remaining = {key: 0 for key in all_ops}
            children = {}
            for op_id, op in all_ops.items():
                for parent in op["parents"]:
                    if parent in all_ops:
                        if all_ops[parent]["entity_id"] != op["entity_id"]:
                            raise ValueError("父版本属于其他实体")
                        remaining[op_id] += 1
                        children.setdefault(parent, []).append(op_id)
            ready = [key for key, count in remaining.items() if count == 0]
            visited = 0
            while ready:
                key = ready.pop()
                visited += 1
                for child in children.get(key, []):
                    remaining[child] -= 1
                    if remaining[child] == 0:
                        ready.append(child)
            if visited != len(all_ops):
                raise ValueError("同步版本存在循环")
        return {"accepted": accepted, "duplicates": len(operations) - accepted, "known_ids": self.operation_ids()}

    def conflicts(self):
        return [item for item in self.projects() + self.entries(include_done=True) if item["conflict"]]

    def resolve(self, entity_id, data, parents):
        versions = self._versions(entity_id)
        if not versions:
            raise KeyError(entity_id)
        return self._write(entity_id, versions[0]["entity_type"], data, parents)

    @staticmethod
    def _validate_hash(value):
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("无效附件哈希")

    def add_blob(self, content):
        if not isinstance(content, bytes):
            raise ValueError("附件必须是 bytes")
        digest = hashlib.sha256(content).hexdigest()
        folder = self.root / "blobs"
        folder.mkdir(exist_ok=True)
        path = folder / digest
        if path.exists():
            self.get_blob(digest)
        else:
            tmp = folder / (digest + "." + _id() + ".tmp")
            try:
                with tmp.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        return {"hash": digest, "size": len(content)}

    def get_blob(self, digest):
        self._validate_hash(digest)
        content = (self.root / "blobs" / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("附件校验失败")
        return content


# One connection is shared by UI calls and background jobs. Serialize complete reads
# as well as transactions so no caller can observe a half-ingested batch.
def _synchronized(method):
    from functools import wraps
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


for _method in ("info", "join_library", "projects", "get_project", "create_project", "save_project", "bind_project", "unbind_project", "subscribe", "entries", "get_entry", "save_entry", "operation_ids", "export_operations", "ingest_operations", "conflicts", "resolve", "add_blob", "get_blob"):
    setattr(MemoryStore, _method, _synchronized(getattr(MemoryStore, _method)))
