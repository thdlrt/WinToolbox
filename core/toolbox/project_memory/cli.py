"""Small stdlib CLI usable independently on disconnected SSH hosts."""
from __future__ import annotations

import argparse
import base64
import json
import sys
import uuid
from pathlib import Path

from .store import MemoryStore, state_directory


def exchange(store, request):
    if not isinstance(request, dict):
        raise ValueError("交换请求必须为 JSON 对象")
    library = request.get("library_id")
    if library and library != store.library_id:
        store.join_library(library)
    incoming = request.get("blobs", {})
    if not isinstance(incoming, dict):
        raise ValueError("blobs 必须是对象")
    for digest, encoded in incoming.items():
        store._validate_hash(digest)
        content = base64.b64decode(encoded, validate=True)
        import hashlib
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("交换附件哈希不一致")
        store.add_blob(content)
    for op in request.get("operations", []):
        for item in op.get("data", {}).get("attachments", []):
            try:
                store.get_blob(item["hash"])
            except FileNotFoundError as exc:
                raise ValueError("同步记录缺少附件，未确认接收: " + item["hash"]) from exc
    result = store.ingest_operations(request.get("operations", []))
    operations = store.export_operations(request.get("known_ids", []))
    project_ids = request.get("project_ids")
    if project_ids is not None:
        operations = [op for op in operations if op["entity_type"] == "project" or op["scope"] == "global" or op.get("project_id") in project_ids]
    blobs = {}
    missing_blobs = []
    for op in operations:
        for item in op["data"].get("attachments", []):
            digest = item["hash"]
            try:
                blobs[digest] = base64.b64encode(store.get_blob(digest)).decode("ascii")
            except FileNotFoundError:
                missing_blobs.append(digest)
    return {**result, "library_id": store.library_id, "operations": operations, "blobs": blobs,
            "missing_blobs": sorted(set(missing_blobs)), "device_id": store.device_id}


def parser():
    result = argparse.ArgumentParser(description="WinToolbox 项目记忆（支持离线）")
    result.add_argument("--root", "--store", dest="root", default=str(state_directory() / "memory"))
    result.add_argument("--device-id", help=argparse.SUPPRESS)
    result.add_argument("--json", action="store_true", help="输出 JSON（默认）")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    init = commands.add_parser("init")
    init.add_argument("--project", required=True)
    init.add_argument("--name")
    init.add_argument("--project-id")
    init.add_argument("--library-id")
    init.add_argument("--context-dir", choices=[".agent", ".agents"])
    recall = commands.add_parser("recall")
    recall.add_argument("--project")
    recall.add_argument("--query", default="")
    recall.add_argument("--limit", type=int, default=10)
    recall.add_argument("--max-chars", type=int, default=12000)
    read = commands.add_parser("read")
    read.add_argument("id")
    capture = commands.add_parser("capture")
    capture.add_argument("--project")
    capture.add_argument("--title")
    capture.add_argument("--input", help="JSON 文件或 - 从 stdin 读取完整记录；现有记录附 parents")
    capture.add_argument("--body")
    capture.add_argument("--body-file")
    capture.add_argument("--kind", choices=["task", "knowledge"])
    capture.add_argument("--scope", choices=["project", "global"])
    capture.add_argument("--type", dest="knowledge_type")
    capture.add_argument("--status", choices=["active", "blocked", "done"])
    capture.add_argument("--id")
    capture.add_argument("--parents", help="JSON 版本 ID 数组；更新现有记录必填")
    promote = commands.add_parser("promote")
    promote.add_argument("id")
    curate = commands.add_parser("curate")
    curate.add_argument("id")
    curate.add_argument("--decision", choices=["accept", "reject"], required=True)
    curate.add_argument("--reason", required=True)
    commands.add_parser("doctor")
    commands.add_parser("exchange")
    return result


def project_id(store, value):
    if not value:
        return None
    path = Path(value)
    if path.is_dir():
        for folder in (".agent", ".agents"):
            manifest = path / folder / "toolbox.json"
            if manifest.is_file():
                linked = json.loads(manifest.read_text(encoding="utf-8"))
                if linked.get("library_id") != store.library_id:
                    raise ValueError("项目属于其他知识库，请显式执行 init 关联正确知识库")
                return linked["project_id"]
        raise ValueError("项目尚未初始化，请先执行 init --project PATH")
    return value


def run(store, args):
    command = args.command
    if command == "info":
        return store.info()
    if command == "exchange":
        return exchange(store, json.load(sys.stdin))
    if command == "init":
        path = Path(args.project).expanduser().resolve()
        linked = next((path / folder / "toolbox.json" for folder in (".agent", ".agents") if (path / folder / "toolbox.json").is_file()), None)
        if linked:
            manifest = json.loads(linked.read_text(encoding="utf-8"))
            store.join_library(manifest["library_id"])
            pid = manifest["project_id"]
        else:
            if args.library_id:
                store.join_library(args.library_id)
            pid = args.project_id
        try:
            project = store.get_project(pid) if pid else None
        except KeyError:
            project = None
        project = project or store.create_project(args.name or path.name, pid)
        location = store.bind_project(project["id"], path, context_dir=args.context_dir)
        from .principles import initialize_project_rules
        principles = initialize_project_rules(store, path)
        return {"project": project, "location": location, "principles": principles, **store.info()}
    if command == "recall":
        pid = project_id(store, args.project)
        rows = store.entries(query=args.query)
        rows = [row for row in rows if not row.get("internal") and not row.get("archived") and row.get("promotion", {}).get("state") not in ("candidate", "rejected")
                and (pid is None or row.get("project_id") == pid or row.get("scope") == "global")]
        rows.sort(key=lambda row: (0 if pid and row.get("project_id") == pid else 1, row["title"], row["id"]))
        selected = []
        remaining = max(0, args.max_chars)
        for row in rows[:max(0, args.limit)]:
            row = dict(row)
            row.pop("versions", None)
            row["body"] = row["body"][:remaining]
            remaining -= len(row["body"])
            selected.append(row)
            if remaining <= 0:
                break
        return {"entries": selected, "total": len(rows)}
    if command == "read":
        return store.get_entry(args.id)
    if command == "capture":
        incoming = {}
        if args.input:
            incoming = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text(encoding="utf-8"))
            if not isinstance(incoming, dict):
                raise ValueError("输入记录必须为 JSON 对象")
        explicit_parents = json.loads(args.parents) if args.parents is not None else incoming.pop("parents", None)
        entry_id = args.id or incoming.get("id")
        data = {}
        if entry_id:
            try:
                data = store.get_entry(entry_id)
            except KeyError:
                pass
            if data and explicit_parents is None:
                raise ValueError("更新现有记录必须提供 --parents 或输入 parents")
        data.update(incoming)
        for key in ("title", "body", "kind", "scope", "knowledge_type", "status"):
            if getattr(args, key) is not None:
                data[key] = getattr(args, key)
        if args.body_file:
            data["body"] = Path(args.body_file).read_text(encoding="utf-8")
        if args.project is not None:
            data["project_id"] = project_id(store, args.project)
        data.setdefault("kind", "knowledge")
        data.setdefault("scope", "project")
        if entry_id:
            data["id"] = entry_id
        return store.save_entry(data, explicit_parents)
    if command == "promote":
        source = store.get_entry(args.id)
        if source["kind"] != "knowledge" or source["scope"] != "project" or source["conflict"]:
            raise ValueError("请选择没有冲突的项目知识进行提升")
        stable = json.dumps([source["id"], sorted(source["heads"])])
        candidate_id = "ak:knowledge:" + str(uuid.uuid5(uuid.NAMESPACE_URL, stable))
        try:
            return store.get_entry(candidate_id)
        except KeyError:
            pass
        data = {key: value for key, value in source.items() if key not in ("id", "heads", "versions", "conflict", "pending_count")}
        data.update(id=candidate_id, title="待整理：" + source["title"], scope="global", project_id=None,
                    related_ids=list(dict.fromkeys([source["id"], *source.get("related_ids", [])])),
                    promotion={"state": "candidate", "source_id": source["id"], "source_project_id": source["project_id"], "source_heads": source["heads"]})
        return store.save_entry(data)
    if command == "curate":
        record = store.get_entry(args.id)
        promotion = record.get("promotion", {})
        if promotion.get("state") != "candidate" or record["conflict"]:
            raise ValueError("记录不是待审核候选或存在冲突")
        source = store.get_entry(promotion["source_id"])
        if source["conflict"] or sorted(source["heads"]) != sorted(promotion.get("source_heads", [])):
            raise ValueError("源知识已变化，请重新提升当前版本")
        record["promotion"] = {**promotion, "state": "accepted" if args.decision == "accept" else "rejected",
                               "decision": {"action": args.decision, "reason": args.reason}, "reviewer": "cli",
                               "reviewed_heads": record["heads"], "reviewed_source_heads": source["heads"], "reviewer_device_id": store.device_id}
        if args.decision == "reject":
            record["archived"] = True
        elif record["title"].startswith("待整理："):
            record["title"] = record["title"][4:]
        return store.save_entry(record, record["heads"])
    if command == "doctor":
        missing = []
        for op in store.export_operations():
            for blob in op["data"].get("attachments", []):
                try:
                    store.get_blob(blob["hash"])
                except (FileNotFoundError, ValueError):
                    missing.append(blob["hash"])
        ids = set(store.operation_ids())
        pending = sorted({p for op in store.export_operations() for p in op["parents"] if p not in ids})
        return {**store.info(), "conflicts": len(store.conflicts()), "missing_blobs": sorted(set(missing)), "missing_parents": pending,
                "ok": not missing and not pending and not store.conflicts()}
    raise ValueError("未知命令")


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args([arg for arg in (argv if argv is not None else sys.argv[1:]) if arg != "--json"])
    store = None
    try:
        store = MemoryStore(Path(args.root), device_id=args.device_id)
        result = run(store, args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
