"""Small meeting libraries are indexed ahead of time and reused at question time."""
import threading
import time

import pytest

from toolbox.app import App
from toolbox.features import knowledge


def wait(app, job, expected="completed"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = app.jobs.get(job["id"])
        if result["status"] not in ("queued", "running", "cancelling") and job["id"] not in app.jobs.active:
            assert result["status"] == expected, result
            return result
        time.sleep(.01)
    raise AssertionError("Index fixture job timed out")


@pytest.fixture
def app(tmp_path, monkeypatch):
    instance = App(tmp_path / "data", register_live=False)
    calls = []
    def embed(texts):
        calls.append(list(texts))
        return [[1.0, .2] for _ in texts]
    monkeypatch.setattr(instance.providers, "embed", embed)
    instance.embedding_calls = calls
    yield instance
    instance.close()
    instance.jobs.pool.shutdown(wait=True)


def library(app, tmp_path):
    collection = app.call("knowledge.create", {"name": "Meeting references"})
    path = tmp_path / "facts.txt"
    path.write_text("The Moon is 384,400 kilometers away from Earth.", encoding="utf-8")
    return collection["id"], path


def test_index_readiness_and_search_reuse_persisted_document_vectors(app, tmp_path, monkeypatch):
    cid, path = library(app, tmp_path)
    assert app.call("knowledge.index_status", {"collection_id": cid})["status"] == "empty"
    assert not app.embedding_calls
    wait(app, app.call("knowledge.ingest", {"collection_id": cid, "paths": [str(path)], "visual": False}))
    state = app.call("knowledge.index_status", {"collection_id": cid})
    assert state["status"] == "ready" and state["ready"] and state["mode"] == "hybrid"
    assert state["document_count"] == state["chunk_count"] == state["indexed_chunk_count"] == 1
    assert len(app.embedding_calls) == 1
    query = "What is the distance from Earth to the Moon?"
    answer = app.call("knowledge.search", {"collection_id": cid, "query": query})
    assert answer["mode"] == "hybrid" and answer["results"]
    assert app.embedding_calls[-1] == [query] and len(app.embedding_calls) == 2
    # Restarting only reads persisted document vectors, not the source file.
    reopened = App(app.data_dir, register_live=False)
    query_calls = []
    monkeypatch.setattr(reopened.providers, "embed", lambda texts: query_calls.append(texts) or [[1.0, .2]])
    try:
        assert reopened.call("knowledge.index_status", {"collection_id": cid})["ready"]
        assert not query_calls
        assert reopened.call("knowledge.search", {"collection_id": cid, "query": query})["mode"] == "hybrid"
        assert query_calls == [[query]]
    finally:
        reopened.close()


def test_model_change_reindexes_saved_chunks_without_reparsing(app, tmp_path, monkeypatch):
    cid, path = library(app, tmp_path)
    wait(app, app.call("knowledge.ingest", {"collection_id": cid, "paths": [str(path)], "visual": False}))
    app.settings.update({"roles": {"embedding": {"model": "different-fixture-model"}}})
    state = app.call("knowledge.index_status", {"collection_id": cid})
    assert state["status"] == "needs-index" and not state["ready"] and state["indexed_chunk_count"] == 0
    path.unlink()
    monkeypatch.setattr(knowledge, "extract_document", lambda *args: pytest.fail("Reindex must not parse documents again"))
    result = wait(app, app.call("knowledge.reindex", {"collection_id": cid}))
    assert result["result"]["indexed_chunks"] == 1
    assert app.call("knowledge.index_status", {"collection_id": cid})["ready"]
    count = len(app.embedding_calls)
    cached = wait(app, app.call("knowledge.reindex", {"collection_id": cid}))
    assert cached["result"]["cached"] and len(app.embedding_calls) == count


def test_embedding_failure_is_keyword_only_until_preindex_succeeds(app, tmp_path, monkeypatch):
    cid, path = library(app, tmp_path)
    def unavailable(_):
        raise ValueError("Embedding is not configured")
    monkeypatch.setattr(app.providers, "embed", unavailable)
    wait(app, app.call("knowledge.ingest", {"collection_id": cid, "paths": [str(path)], "visual": False}))
    state = app.call("knowledge.index_status", {"collection_id": cid})
    assert state["status"] == "keyword-only" and not state["ready"] and state["warnings"]
    wait(app, app.call("knowledge.reindex", {"collection_id": cid}), expected="failed")
    assert app.call("knowledge.index_status", {"collection_id": cid})["status"] == "keyword-only"
    monkeypatch.setattr(app.providers, "embed", lambda texts: [[1.0, .2] for _ in texts])
    wait(app, app.call("knowledge.reindex", {"collection_id": cid}))
    state = app.call("knowledge.index_status", {"collection_id": cid})
    assert state["ready"] and not state["warnings"]
    assert app.call("knowledge.documents", {"collection_id": cid})["documents"][0]["status"] == "ready"


def test_index_status_reports_active_job_without_calling_model(app, tmp_path, monkeypatch):
    cid, path = library(app, tmp_path)
    entered, release = threading.Event(), threading.Event()
    def waiting(texts):
        entered.set()
        assert release.wait(3)
        return [[1.0, .2] for _ in texts]
    monkeypatch.setattr(app.providers, "embed", waiting)
    job = app.call("knowledge.ingest", {"collection_id": cid, "paths": [str(path)], "visual": False})
    try:
        assert entered.wait(2)
        state = app.call("knowledge.index_status", {"collection_id": cid})
        assert state["status"] == "indexing" and not state["ready"] and state["job_id"] == job["id"]
        assert 0 <= state["progress"] <= 100
    finally:
        release.set()
    wait(app, job)
    assert app.call("knowledge.index_status", {"collection_id": cid})["ready"]


def test_partly_indexed_library_reports_incomplete_not_ready(app, tmp_path, monkeypatch):
    cid, path = library(app, tmp_path)
    wait(app, app.call("knowledge.ingest", {"collection_id": cid, "paths": [str(path)], "visual": False}))
    second = tmp_path / "more-facts.txt"
    second.write_text("The Moon has no rings.", encoding="utf-8")
    def unavailable(_):
        raise ValueError("Temporary embedding failure")
    monkeypatch.setattr(app.providers, "embed", unavailable)
    wait(app, app.call("knowledge.ingest", {"collection_id": cid, "paths": [str(second)], "visual": False}))
    state = app.call("knowledge.index_status", {"collection_id": cid})
    assert state["status"] == "needs-index" and not state["ready"]
    assert state["mode"] == "hybrid" and state["indexed_chunk_count"] == 1 and state["chunk_count"] == 2
    assert "尚未完整" in state["message"] and state["warnings"]
