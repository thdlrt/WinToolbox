import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from toolbox.project_memory import MemoryStore
from toolbox.project_memory.cli import exchange, parser, run


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a = MemoryStore(self.root / "a", device_id="device-a")
        self.b = MemoryStore(self.root / "b", device_id="device-b")
        self.b.join_library(self.a.library_id)
        self.project = self.a.create_project("测试项目")

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    def capture(self, **overrides):
        return self.a.save_entry({"title": "经验", "body": "已验证", "kind": "knowledge", "scope": "project", "project_id": self.project["id"], **overrides})

    def test_offline_concurrent_updates_survive_and_resolve(self):
        initial = self.capture()
        self.b.ingest_operations(self.a.export_operations())
        left = self.a.save_entry({**initial, "body": "电脑 A 修改"}, initial["heads"])
        right = self.b.save_entry({**initial, "body": "服务器离线修改"}, initial["heads"])
        self.a.ingest_operations(self.b.export_operations())
        record = self.a.get_entry(initial["id"])
        self.assertTrue(record["conflict"])
        self.assertEqual({v["data"]["body"] for v in record["versions"]}, {"电脑 A 修改", "服务器离线修改"})
        with self.assertRaises(ValueError):
            self.a.save_entry({**initial, "body": "旧窗口"}, initial["heads"])
        merged = self.a.resolve(initial["id"], {**record, "body": "保留两方结论"}, record["heads"])
        self.b.ingest_operations(self.a.export_operations())
        self.assertFalse(merged["conflict"])
        self.assertEqual(self.b.get_entry(initial["id"])["body"], "保留两方结论")

    def test_out_of_order_and_idempotency(self):
        initial = self.capture()
        updated = self.a.save_entry({**initial, "body": "下一版"}, initial["heads"])
        operations = self.a.export_operations()
        latest = next(op for op in operations if op["op_id"] == updated["heads"][0])
        self.b.ingest_operations([latest])
        self.assertEqual(self.b.entries(), [])
        self.b.ingest_operations(operations)
        self.assertEqual(self.b.get_entry(initial["id"])["body"], "下一版")
        self.assertEqual(self.b.ingest_operations(operations)["accepted"], 0)
        corrupt = copy.deepcopy(latest)
        corrupt["data"]["body"] = "篡改"
        with self.assertRaises(ValueError):
            self.b.ingest_operations([corrupt])

    def test_entire_batch_rolls_back_on_invalid_op(self):
        initial = self.capture()
        operations = self.a.export_operations()
        corrupt = copy.deepcopy(operations[-1])
        corrupt["library_id"] = "other-library"
        with self.assertRaises(ValueError):
            self.b.ingest_operations(operations + [corrupt])
        self.assertEqual(self.b.operation_ids(), [])

    def test_partial_device_has_no_foreign_location(self):
        local = self.root / "project"
        local.mkdir()
        self.a.bind_project(self.project["id"], local)
        self.capture()
        self.b.ingest_operations(self.a.export_operations())
        project = self.b.projects()[0]
        self.assertFalse(project["available"])
        self.assertFalse(project["subscribed"])
        self.assertEqual(project["locations"], [])
        self.assertEqual(len(self.b.entries()), 1)
        self.assertNotIn(str(local), json.dumps(self.a.export_operations()))
        with self.assertRaises(ValueError):
            self.b.bind_project(project["id"], self.root / "missing")
        self.assertFalse((self.root / "missing").exists())

    def test_legacy_device_upgrade_preserves_versions_and_syncs_idempotently(self):
        import sqlite3
        original = self.capture(scope="global", project_id=None, body="原始正文")
        updated = self.a.save_entry({**original, "body": "修改正文"}, original["heads"])
        # Simulate an old database whose private operations have never left this PC.
        with self.a.db:
            for op in self.a._versions(original["id"]):
                op["scope"] = op["data"]["scope"] = "device"
                self.a.db.execute("UPDATE operations SET payload=? WHERE op_id=?", (json.dumps(op), op["op_id"]))
        self.a.reopen()
        upgraded = self.a.get_entry(original["id"])
        self.assertEqual(upgraded["scope"], "global")
        self.assertEqual(upgraded["body"], "修改正文")
        self.assertEqual(upgraded["heads"], updated["heads"])
        versions = self.a._versions(original["id"])
        self.assertEqual(len(versions), 2)
        backups = list((self.root / "project-memory-local/scope-backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            rows = db.execute("SELECT payload FROM operations WHERE entity_id=?", (original["id"],)).fetchall()
            self.assertTrue(all(json.loads(row[0])["scope"] == "device" for row in rows))
        db.close()
        ops = self.a.export_operations()
        self.a.reopen()
        self.assertEqual(self.a.export_operations(), ops)
        self.b.ingest_operations(ops)
        self.b.ingest_operations(ops)
        self.assertEqual(self.b.get_entry(original["id"])["body"], "修改正文")
        self.assertEqual(self.b.get_entry(original["id"])["heads"], updated["heads"])

    def test_device_scope_no_longer_accepted(self):
        with self.assertRaises(ValueError):
            self.capture(scope="device", project_id=None)

    def test_data_copy_uses_new_machine_replica_and_hides_old_locations(self):
        local = self.root / "project"
        local.mkdir()
        self.a.bind_project(self.project["id"], local)
        self.a.db.execute("PRAGMA wal_checkpoint(FULL)")
        shutil.copytree(self.root / "a", self.root / "copied")
        copied = MemoryStore(self.root / "copied", device_id="device-c")
        try:
            self.assertNotEqual(copied.replica_id, self.a.replica_id)
            self.assertEqual(copied.library_id, self.a.library_id)
            self.assertEqual(copied.projects()[0]["locations"], [])
        finally:
            copied.close()

    def test_manifest_conflict_is_not_overwritten(self):
        local = self.root / "project"
        local.mkdir()
        (local / ".agents").mkdir()
        self.a.bind_project(self.project["id"], local)
        manifest = local / ".agents/toolbox.json"
        original = manifest.read_bytes()
        self.assertFalse((local / ".agent").exists())
        second = self.a.create_project("另一个项目")
        with self.assertRaises(ValueError):
            self.a.bind_project(second["id"], local)
        self.assertEqual(manifest.read_bytes(), original)

    def test_knowledge_has_no_task_status_and_done_is_history(self):
        knowledge = self.capture(status="active", knowledge_type="决策")
        self.assertNotIn("status", knowledge)
        self.assertEqual(knowledge["knowledge_type"], "决策")
        task = self.capture(kind="task", title="已完成任务", status="done")
        self.assertEqual(len(self.a.entries()), 1)
        self.assertEqual(len(self.a.entries(include_done=True)), 2)

    def test_attachment_hash_and_exchange_project_selection(self):
        blob = self.a.add_blob(b"attachment")
        self.capture(attachments=[{**blob, "name": "附件.txt"}])
        second = self.a.create_project("其他项目")
        self.capture(project_id=second["id"], title="不订阅")
        response = exchange(self.a, {"library_id": self.a.library_id, "project_ids": [self.project["id"]]})
        self.assertEqual(len([o for o in response["operations"] if o["entity_type"] == "entry"]), 1)
        exchange(self.b, {"library_id": self.a.library_id, "operations": response["operations"], "blobs": response["blobs"]})
        self.assertEqual(self.b.get_blob(blob["hash"]), b"attachment")
        (self.b.root / "blobs" / blob["hash"]).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            self.b.get_blob(blob["hash"])
        with self.assertRaises(ValueError):
            self.a.get_blob("../escape")

    def test_parent_different_entity_and_cycle_rejected(self):
        first = self.capture()
        second = self.capture(title="第二条")
        op = next(o for o in self.a.export_operations() if o["entity_id"] == first["id"])
        op["parents"] = second["heads"]
        with self.assertRaises(ValueError):
            self.b.ingest_operations([o for o in self.a.export_operations() if o["entity_id"] == second["id"]] + [op])
        self.assertEqual(self.b.operation_ids(), [])

    def test_nonempty_library_cannot_silently_join(self):
        with self.assertRaises(ValueError):
            self.a.join_library("wrong")

    def test_scope_transition_and_ambiguous_project_roots_rejected(self):
        entry = self.capture()
        with self.assertRaises(ValueError):
            self.a.save_entry({**entry, "scope": "global", "project_id": None}, entry["heads"])
        root = self.root / "ambiguous"
        (root / ".agent").mkdir(parents=True)
        (root / ".agents").mkdir()
        with self.assertRaises(ValueError):
            self.a.bind_project(self.project["id"], root)
        self.a.bind_project(self.project["id"], root, context_dir=".agent")
        self.assertTrue((root / ".agent/toolbox.json").is_file())
        self.assertFalse((root / ".agents/toolbox.json").exists())
        with self.assertRaises(ValueError):
            self.a.bind_project(self.project["id"], root, context_dir="../outside")

    def test_long_version_chain_and_snapshot_reopen(self):
        import uuid
        template = self.a.export_operations()[0]
        operations = []
        parents = []
        for i in range(1200):
            op = copy.deepcopy(template)
            op["op_id"] = str(uuid.uuid4())
            op["parents"] = parents
            op["data"]["name"] = f"版本 {i}"
            parents = [op["op_id"]]
            operations.append(op)
        self.b.ingest_operations(list(reversed(operations)))
        self.assertEqual(self.b.projects()[0]["name"], "版本 1199")
        self.b.close()
        self.b.reopen()
        self.assertEqual(self.b.projects()[0]["name"], "版本 1199")

    def test_exchange_does_not_ack_record_with_missing_attachment(self):
        blob = self.a.add_blob(b"durable")
        self.capture(attachments=[blob])
        with self.assertRaises(ValueError):
            exchange(self.b, {"library_id": self.a.library_id, "operations": self.a.export_operations()})
        self.assertEqual(self.b.operation_ids(), [])

    def test_cli_metadata_preservation_and_promotion_source_revision(self):
        source = self.capture(related_ids=["another"], legacy={"classification": "important"})
        args = parser().parse_args(["capture", "--id", source["id"], "--body", "新内容", "--parents", json.dumps(source["heads"])])
        updated = run(self.a, args)
        self.assertEqual(updated["legacy"], source["legacy"])
        self.assertEqual(updated["related_ids"], source["related_ids"])
        promoted = run(self.a, parser().parse_args(["promote", source["id"]]))
        self.assertEqual(run(self.a, parser().parse_args(["promote", source["id"]]))["id"], promoted["id"])
        self.a.save_entry({**updated, "body": "再修改"}, updated["heads"])
        with self.assertRaises(ValueError):
            run(self.a, parser().parse_args(["curate", promoted["id"], "--decision", "accept", "--reason", "可复用"]))

    def test_cli_recall_does_not_create_missing_project_and_orders_scopes(self):
        self.capture(title="项目知识")
        self.capture(title="全局知识", scope="global", project_id=None)
        self.capture(title="候选", scope="global", project_id=None, promotion={"state": "candidate"})
        result = run(self.a, parser().parse_args(["recall", "--project", self.project["id"]]))
        self.assertEqual([r["scope"] for r in result["entries"]], ["project", "global"])
        project = self.root / "foreign"
        (project / ".agent").mkdir(parents=True)
        (project / ".agent/toolbox.json").write_text(json.dumps({"library_id": "another", "project_id": "absent"}))
        before = self.b.info()
        with self.assertRaises(ValueError):
            run(self.b, parser().parse_args(["recall", "--project", str(project)]))
        self.assertEqual(self.b.info(), before)

    def test_cli_exchange_stdout_json_and_offline_capture(self):
        project = self.root / "cli-project"
        project.mkdir()
        root = self.root / "cli-store"
        prefix = [sys.executable, "-m", "toolbox.project_memory", "--store", str(root), "--device-id", "test-cli"]
        result = subprocess.run(prefix + ["init", "--project", str(project)], capture_output=True, text=True, encoding="utf-8", check=True)
        info = json.loads(result.stdout)
        result = subprocess.run(prefix + ["capture", "--project", str(project), "--title", "离线记录", "--body", "恢复网络后同步"], capture_output=True, text=True, encoding="utf-8", check=True)
        entry = json.loads(result.stdout)
        result = subprocess.run(prefix + ["exchange"], input=json.dumps({"library_id": info["library_id"]}), capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(len(json.loads(result.stdout)["operations"]), 2)
        self.assertEqual(entry["title"], "离线记录")


if __name__ == "__main__":
    unittest.main()
