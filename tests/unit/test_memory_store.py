"""中期版本表、长期条目与 spans 迁移测试（SPEC-0013 §5.7 / §8.3）。"""

from __future__ import annotations

import asyncio
import sqlite3

from devops_agent.memory.episodic import EpisodicMemory
from devops_agent.memory.long_term import build_long_term
from devops_agent.memory_policy import EpisodicPolicy, LongTermPolicy
from devops_agent.observability import repository as repo
from devops_agent.observability.models import SpanRecord


def test_episodic_versions_switch_active_and_prune(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "obs.db"
    memory = EpisodicMemory(EpisodicPolicy(max_versions=2), db)
    first = memory.save(
        "s1", text="第一版", trigger="t", tokens_before=100, tokens_after=40, budget=80
    )
    second = memory.save(
        "s1", text="第二版", trigger="t", tokens_before=120, tokens_after=50, budget=80
    )
    assert first["version"] == 1
    assert second["version"] == 2
    assert memory.active("s1")["version"] == 2  # type: ignore[index]
    third = memory.save(
        "s1", text="第三版", trigger="t", tokens_before=140, tokens_after=60, budget=80
    )
    versions = memory.versions("s1", 10)
    assert third["version"] == 3
    assert [item["version"] for item in versions] == [3, 2]
    assert memory.active("s1")["text"] == "第三版"  # type: ignore[index]


def test_episodic_isolation_and_delete(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "obs.db"
    memory = EpisodicMemory(EpisodicPolicy(), db)
    card = memory.save(
        "s1", text="只有 s1 能看到", trigger="t", tokens_before=1, tokens_after=1, budget=1
    )
    assert memory.active("s2") is None
    assert memory.delete(int(card["id"])) is True
    assert memory.active("s1") is None


def test_long_term_write_search_dedupe_and_isolation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "obs.db"
    policy = LongTermPolicy(backend="local")
    store, backend = build_long_term(policy, "session:s1", db)
    assert backend == "local"
    first = asyncio.run(store.write("用户更喜欢用 pytest 而不是 unittest"))
    assert first["stored"] is True
    second = asyncio.run(store.write("用户更喜欢用 pytest 而不是 unittest"))
    assert second["deduped"] is True

    hits = asyncio.run(store.search("pytest", top_k=5, min_score=0.1))
    assert hits and "pytest" in hits[0].text

    other, _ = build_long_term(policy, "session:s2", db)
    assert asyncio.run(other.search("pytest", top_k=5, min_score=0.1)) == []


def test_external_backends_degrade_to_off(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "obs.db"
    store, backend = build_long_term(LongTermPolicy(backend="mem0"), "session:s1", db)
    assert backend == "off"
    assert asyncio.run(store.search("anything", 5, 0.1)) == []
    assert asyncio.run(store.write("x"))["stored"] is False


def test_old_span_check_is_migrated_and_rows_preserved(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """老库 CHECK 不含 memory：升级后旧行可读，新 memory span 可写。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            domain TEXT NOT NULL, status TEXT NOT NULL, input TEXT NOT NULL,
            output TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, ended_at TEXT,
            duration_ms INTEGER, model TEXT NOT NULL DEFAULT '', total_tokens INTEGER
                NOT NULL DEFAULT 0, health_score REAL, request_id TEXT);
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            type TEXT NOT NULL CHECK (type IN ('model', 'tool', 'permission')),
            name TEXT NOT NULL, start_offset_ms INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER, input TEXT NOT NULL DEFAULT '',
            output TEXT NOT NULL DEFAULT '', tokens INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok', error TEXT);
        INSERT INTO runs (session_id, domain, status, input, started_at)
            VALUES ('s1', 'debug', 'success', 'q', '2026-01-01T00:00:00+00:00');
        INSERT INTO spans (run_id, type, name) VALUES (1, 'tool', 'Bash');
        """
    )
    conn.commit()
    conn.close()

    with repo.connect(db) as conn:
        repo.init_db(conn)
        spans = repo.list_spans(conn, 1)
        assert [span.name for span in spans] == ["Bash"]
        repo.add_span(
            conn,
            SpanRecord(
                id=None,
                run_id=1,
                type="memory",
                name="memory.trim",
                start_offset_ms=10,
                duration_ms=2,
                input="",
                output="{}",
                tokens=0,
                status="ok",
                error=None,
            ),
        )
        assert [span.type for span in repo.list_spans(conn, 1)] == ["tool", "memory"]
