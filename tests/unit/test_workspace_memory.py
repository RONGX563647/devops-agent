"""工作区记忆验收（SPEC-0021 A7/A8/A9/A15）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from devops_agent.config_schema import HealthPolicy
from devops_agent.memory.long_term import LocalLongTerm
from devops_agent.memory.scope import effective_scope, resolve_scope_id
from devops_agent.memory_policy import MemoryPolicy
from devops_agent.observability import repository as repo
from devops_agent.observability.models import RunRecord


def test_scope_resolution_prefers_workspace() -> None:
    """绑定时强制 workspace；未绑定保持旧行为。"""
    policy = MemoryPolicy()
    assert effective_scope(policy, "ws_1") == "workspace"
    assert resolve_scope_id(policy, "s1", "ws_1") == "workspace:ws_1"
    assert effective_scope(policy, None) == "session"
    assert resolve_scope_id(policy, "s1", None) == "session:s1"


def test_same_workspace_shares_long_term(tmp_path: Path) -> None:
    """A8：同工作区会话 A 写入，会话 B 能召回（同一 scope_id）。"""
    db = tmp_path / "obs.db"
    policy = MemoryPolicy()
    writer = LocalLongTerm(policy.long_term, "workspace:ws_1", db, scope="workspace")
    reader = LocalLongTerm(policy.long_term, "workspace:ws_1", db, scope="workspace")
    asyncio.run(writer.write("部署脚本必须使用 uv lock 安装依赖"))
    hits = asyncio.run(reader.search("uv lock 安装", 5, 0.0))
    assert hits and "uv lock" in hits[0].text


def test_cross_workspace_long_term_is_zero(tmp_path: Path) -> None:
    """A9 硬红线：两个工作区互相检索命中数为 0。"""
    db = tmp_path / "obs.db"
    policy = MemoryPolicy()
    alpha = LocalLongTerm(policy.long_term, "workspace:ws_a", db, scope="workspace")
    beta = LocalLongTerm(policy.long_term, "workspace:ws_b", db, scope="workspace")
    asyncio.run(alpha.write("alpha 仓库使用 pnpm"))
    assert asyncio.run(beta.search("pnpm", 10, 0.0)) == []
    assert len(asyncio.run(alpha.search("pnpm", 10, 0.0))) == 1


def test_session_memories_never_cross_sessions(tmp_path: Path) -> None:
    """A7：中期卡片按 session_id 隔离，同工作区另一会话读不到。"""
    db = tmp_path / "obs.db"
    with repo.connect(db) as conn:
        repo.init_db(conn)
        repo.save_memory_version(
            conn,
            session_id="s1",
            version=1,
            trigger="context_threshold",
            text="会话一的中期摘要",
            artifacts=[],
            pending=[],
            errors=[],
            tokens_before=100,
            tokens_after=40,
            budget=80,
            source_run_ids=[],
            ttl_days=30,
            max_versions=10,
        )
        assert repo.active_memory(conn, "s1") is not None
        assert repo.active_memory(conn, "s2") is None


def test_runs_carry_workspace_id_and_filter(tmp_path: Path) -> None:
    """A15：runs.workspace_id 正确落库，可按工作区查询。"""
    db = tmp_path / "obs.db"
    with repo.connect(db) as conn:
        repo.init_db(conn)
        created = repo.create_run(
            conn,
            RunRecord(
                id=None,
                session_id="s1",
                domain="debug",
                status="running",
                input="hi",
                output="",
                started_at="2026-01-01T00:00:00+00:00",
                ended_at=None,
                duration_ms=None,
                model="m",
                total_tokens=0,
                health_score=None,
                workspace_id="ws_1",
            ),
        )
        other = repo.create_run(
            conn,
            RunRecord(
                id=None,
                session_id="s2",
                domain="debug",
                status="running",
                input="hi",
                output="",
                started_at="2026-01-01T00:00:01+00:00",
                ended_at=None,
                duration_ms=None,
                model="m",
                total_tokens=0,
                health_score=None,
                workspace_id="ws_2",
            ),
        )
        rows = repo.query_runs(conn, workspace_id="ws_1")
        assert [row.id for row in rows] == [created]
        assert repo.count_runs(conn, workspace_id="ws_1") == 1
        assert repo.get_run(conn, other).workspace_id == "ws_2"


def test_health_policy_unaffected() -> None:
    """守卫：新增配置域不影响既有默认。"""
    assert HealthPolicy().weights.execution > 0
