import contextlib
import datetime
import json
import sqlite3
import threading
import time
from pathlib import Path


class Storage:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "engine.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("此数据目录由较新版本创建，请更新 WinToolbox，避免旧版本修改数据。")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints(key TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_usage(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    role TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    audio_seconds REAL NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    usage_reported INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS api_usage_provider_time ON api_usage(provider_id,created_at);
                PRAGMA user_version=2;
            """)

    @contextlib.contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            try:
                yield db
                db.commit()
            finally:
                db.close()

    def put(self, table, key, value):
        assert table in ("jobs", "checkpoints")
        column = "id" if table == "jobs" else "key"
        with self.connect() as db:
            db.execute(f"INSERT OR REPLACE INTO {table}({column},record) VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def get(self, table, key):
        assert table in ("jobs", "checkpoints")
        column = "id" if table == "jobs" else "key"
        with self.connect() as db:
            row = db.execute(f"SELECT record FROM {table} WHERE {column}=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table):
        assert table in ("jobs", "checkpoints")
        with self.connect() as db:
            rows = db.execute(f"SELECT record FROM {table}").fetchall()
        return [json.loads(row[0]) for row in rows]

    def record_usage(self, provider_id, model, role, operation, status="success", *, usage=None,
                     audio_seconds=0, latency_ms=0):
        """Persist counters only. Prompts, responses and credentials are never stored."""
        usage = usage if isinstance(usage, dict) else {}
        def number(*names):
            for name in names:
                value = usage.get(name)
                if isinstance(value, (int, float)) and value >= 0:
                    return int(value)
            return 0
        input_tokens = number("prompt_tokens", "input_tokens", "promptTokenCount", "inputTokenCount")
        output_tokens = number("completion_tokens", "output_tokens", "candidatesTokenCount", "outputTokenCount")
        total_tokens = number("total_tokens", "totalTokenCount") or input_tokens + output_tokens
        reported = any(name in usage for name in ("prompt_tokens", "input_tokens", "promptTokenCount", "inputTokenCount",
                                                   "completion_tokens", "output_tokens", "candidatesTokenCount", "outputTokenCount",
                                                   "total_tokens", "totalTokenCount"))
        with self.connect() as db:
            db.execute("""INSERT INTO api_usage(created_at,provider_id,model,role,operation,status,input_tokens,
                output_tokens,total_tokens,audio_seconds,latency_ms,usage_reported) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), str(provider_id), str(model), str(role), str(operation), str(status), input_tokens,
                 output_tokens, total_tokens, max(0.0, float(audio_seconds or 0)), max(0, int(latency_ms or 0)), int(reported)))

    def usage_summary(self, provider_id):
        now = datetime.datetime.now().astimezone()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        def aggregate(start):
            clause, values = "provider_id=?", [provider_id]
            if start is not None:
                clause += " AND created_at>=?"
                values.append(start)
            with self.connect() as db:
                row = db.execute(f"""SELECT COUNT(*) requests,
                    COALESCE(SUM(status='success'),0) successful,
                    COALESCE(SUM(status='error'),0) failed,
                    COALESCE(SUM(status='cancelled'),0) cancelled,
                    COALESCE(SUM(input_tokens),0) input_tokens,
                    COALESCE(SUM(output_tokens),0) output_tokens,
                    COALESCE(SUM(total_tokens),0) total_tokens,
                    COALESCE(SUM(audio_seconds),0) audio_seconds,
                    COALESCE(SUM(usage_reported),0) token_reported_requests,
                    COALESCE(SUM(latency_ms),0) latency_ms
                    FROM api_usage WHERE {clause}""", values).fetchone()
            names = ("requests", "successful", "failed", "cancelled", "input_tokens", "output_tokens", "total_tokens",
                     "audio_seconds", "token_reported_requests", "latency_ms")
            result = dict(zip(names, row))
            result["audio_seconds"] = round(result["audio_seconds"], 2)
            result["average_latency_ms"] = round(result.pop("latency_ms") / result["requests"]) if result["requests"] else 0
            return result
        with self.connect() as db:
            rows = db.execute("""SELECT model,operation,COUNT(*) requests,COALESCE(SUM(total_tokens),0) total_tokens,
                COALESCE(SUM(audio_seconds),0) audio_seconds FROM api_usage
                WHERE provider_id=? AND created_at>=? GROUP BY model,operation ORDER BY requests DESC,model LIMIT 12""",
                (provider_id, month)).fetchall()
        return {"provider_id": provider_id, "source": "local", "started_at": self._usage_started(provider_id),
                "periods": {"today": aggregate(today), "month": aggregate(month), "all": aggregate(None)},
                "by_model": [{"model": row[0], "operation": row[1], "requests": row[2], "total_tokens": row[3],
                              "audio_seconds": round(row[4], 2)} for row in rows]}

    def _usage_started(self, provider_id):
        with self.connect() as db:
            row = db.execute("SELECT MIN(created_at) FROM api_usage WHERE provider_id=?", (provider_id,)).fetchone()
        return row[0] if row and row[0] is not None else None
