"""记忆管理 API、上下文分区与 /health 测试（SPEC-0013 §5.9 / §11）。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from devops_agent.config import load_memory
from devops_agent.gateway.app import create_app
from devops_agent.memory.episodic import EpisodicMemory
from devops_agent.memory.long_term import build_long_term
from devops_agent.memory_policy import EpisodicPolicy, LongTermPolicy
from devops_agent.runtime.runtime import EchoRuntime


def _client(tmp_path: Any, monkeypatch: Any) -> TestClient:
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "obs.db"))
    return TestClient(create_app(runtime=EchoRuntime()))


def test_memory_api_crud_and_status(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _client(tmp_path, monkeypatch)
    status = client.get("/api/memory/status").json()
    assert status["short_term"] is True
    assert "backend" in status
    # 开关与后端名只说明「打算做什么」；条数才说明「真的存下来了没有」
    assert status["store"] == {"episodic": 0, "long_term": 0}

    card = EpisodicMemory(EpisodicPolicy(), tmp_path / "obs.db").save(
        "s1",
        text="目标：修好登录" + "x" * 300,
        trigger="t",
        tokens_before=100,
        tokens_after=20,
        budget=80,
    )
    body = client.get("/api/memory/session/s1").json()
    assert body["active"]["version"] == 1
    assert len(body["active"]["text"]) <= load_memory().trace.summary_preview_chars

    assert client.delete(f"/api/memory/episodic/{card['id']}").json()["deleted"] is True
    assert client.get("/api/memory/session/s1").json()["active"] is None
    assert client.delete("/api/memory/nope/1").status_code == 404


def test_long_term_api_lists_and_deletes(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    client = _client(tmp_path, monkeypatch)
    store = build_long_term(LongTermPolicy(backend="local"), "session:s1", tmp_path / "obs.db")
    entry = asyncio.run(store.backend.write("用户更喜欢 pytest"))
    body = client.get("/api/memory/long-term?session_id=s1").json()
    assert body["total"] == 1
    assert body["items"][0]["text"] == "用户更喜欢 pytest"
    assert client.delete(f"/api/memory/long/{entry['id']}").json()["deleted"] is True
    assert client.get("/api/memory/long-term?session_id=s1").json()["total"] == 0


def test_context_api_exposes_zones_budget_and_snapshot(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/context?session=s1").json()
    assert body["budget"]["window"] > 0 and body["budget"]["history_max"] >= 0
    assert body["zones"]["prefix"]["system_fingerprint"]
    assert "snapshot" in body and "memory" in body


def test_health_includes_memory_and_context(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _client(tmp_path, monkeypatch)
    body = client.get("/health").json()
    assert body["memory"]["backend"] in ("local", "off")
    # 探针要能回答「表里到底有没有内容」，不只是「开关开没开」
    assert body["memory"]["store"] == {"episodic": 0, "long_term": 0}
    assert body["context"]["snapshot_version"] >= 1
