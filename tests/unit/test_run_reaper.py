"""悬挂 run 回收：进程没跑到 finish() 的记录不能永远停在 running。

线上库里 10 条 `running` 行跨了 5 天：进程被杀（`--reload`、Ctrl-C、崩溃）时
`finally` 不保证跑到，这些行让失败率被算低、前端一直显示「进行中」，还没有
`ended_at` 去污染耗时统计。

两条边界是本文件重点：太新的不碰（可能是别的进程正在跑的轮次），已收尾的不碰。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devops_agent.observability import repository as repo
from devops_agent.observability.models import RunRecord
from devops_agent.observability.recovery import ORPHAN_REASON, reap_orphan_runs

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def _record(session: str, started_at: str) -> RunRecord:
    return RunRecord(
        id=None,
        session_id=session,
        domain="debug",
        status="running",
        input="q",
        output="",
        started_at=started_at,
        ended_at=None,
        duration_ms=None,
        model="m",
        total_tokens=0,
        health_score=None,
    )


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "obs.db"
    with repo.connect(path) as conn:
        repo.init_db(conn)
    yield path


def _add(db: Path, session: str, started_at: str) -> int:
    with repo.connect(db) as conn:
        return repo.create_run(conn, _record(session, started_at))


def _status(db: Path, run_id: int) -> dict[str, object]:
    with repo.connect(db) as conn:
        row = conn.execute(
            "SELECT status, ended_at, duration_ms, error FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    return dict(row)


def test_old_running_row_is_closed(db: Path) -> None:
    run_id = _add(db, "stuck", _iso(timedelta(days=4)))
    assert reap_orphan_runs(db, now=NOW) == 1
    row = _status(db, run_id)
    assert row["status"] == "failed"
    assert row["ended_at"] == NOW.isoformat()
    assert row["error"] == ORPHAN_REASON
    assert int(row["duration_ms"]) == 4 * 24 * 3600 * 1000


def test_recent_running_row_is_left_alone(db: Path) -> None:
    """刚起的 run 可能是别的进程正在跑的轮次，回收它就是篡改别人的观测数据。"""
    run_id = _add(db, "live", _iso(timedelta(minutes=1)))
    assert reap_orphan_runs(db, now=NOW) == 0
    row = _status(db, run_id)
    assert row["status"] == "running"
    assert row["ended_at"] is None


def test_finished_row_is_not_touched(db: Path) -> None:
    run_id = _add(db, "done", _iso(timedelta(days=3)))
    with repo.connect(db) as conn:
        repo.finish_run(
            conn,
            run_id,
            status="success",
            output="ok",
            ended_at=NOW.isoformat(),
            duration_ms=1234,
            total_tokens=0,
            health_score=None,
        )
    assert reap_orphan_runs(db, now=NOW) == 0
    row = _status(db, run_id)
    assert row["status"] == "success"
    assert row["duration_ms"] == 1234


def test_existing_error_is_preserved(db: Path) -> None:
    """已有失败原因的行不覆盖：回收只补「没写」的那部分。"""
    run_id = _add(db, "with-error", _iso(timedelta(days=2)))
    with repo.connect(db) as conn:
        conn.execute("UPDATE runs SET error='上游 500' WHERE id=?", (run_id,))
    reap_orphan_runs(db, now=NOW)
    assert _status(db, run_id)["error"] == "上游 500"


def test_broken_db_does_not_raise(tmp_path: Path) -> None:
    """观测库不可用不能让服务起不来：回收失败返回 0。"""
    broken = tmp_path / "nope" / "x.db"
    broken.parent.mkdir()
    broken.write_text("not a database", encoding="utf-8")
    assert reap_orphan_runs(broken, now=NOW) == 0
