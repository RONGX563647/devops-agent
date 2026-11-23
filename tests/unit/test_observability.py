"""可观测性存储层单元测试（tmp_path 隔离，不碰真实 workspace）。"""

from pathlib import Path

from devops_agent.observability import repository as repo
from devops_agent.observability.models import EvalScoreRecord, RunRecord, SpanRecord


def _run() -> RunRecord:
    return RunRecord(
        id=None,
        session_id="s1",
        domain="debug",
        status="running",
        input="定位这个 bug",
        output="",
        started_at="2026-11-23T14:00:00+00:00",
        ended_at=None,
        duration_ms=None,
        model="qwen3.8-flash",
        total_tokens=0,
        health_score=None,
    )


def test_default_db_path_uses_env(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv(repo.DB_PATH_ENV, str(tmp_path / "custom.db"))  # type: ignore[attr-defined]
    assert repo.default_db_path() == (tmp_path / "custom.db").resolve()


def test_default_db_path_falls_back() -> None:
    path = repo.default_db_path()
    assert path.name == "observability.db"
    assert path.parent.name == "workspace"


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    with repo.connect(db) as conn:
        repo.init_db(conn)
    with repo.connect(db) as conn:
        repo.init_db(conn)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"runs", "spans", "eval_scores"} <= tables


def test_run_crud_and_finish(tmp_path: Path) -> None:
    with repo.connect(tmp_path / "t.db") as conn:
        repo.init_db(conn)
        run_id = repo.create_run(conn, _run())
        assert run_id == 1

        fetched = repo.get_run(conn, run_id)
        assert fetched is not None
        assert fetched.session_id == "s1"
        assert fetched.status == "running"
        assert fetched.ended_at is None

        repo.finish_run(
            conn,
            run_id,
            status="success",
            output="已定位",
            ended_at="2026-11-23T14:00:03+00:00",
            duration_ms=3000,
            total_tokens=512,
            health_score=88.5,
        )
        done = repo.get_run(conn, run_id)
        assert done is not None
        assert done.status == "success"
        assert done.output == "已定位"
        assert done.duration_ms == 3000
        assert done.total_tokens == 512
        assert done.health_score == 88.5

        listed = repo.list_runs(conn)
        assert len(listed) == 1
        assert listed[0].id == run_id


def test_get_missing_run_returns_none(tmp_path: Path) -> None:
    with repo.connect(tmp_path / "t.db") as conn:
        repo.init_db(conn)
        assert repo.get_run(conn, 999) is None


def test_spans_and_scores_ordering(tmp_path: Path) -> None:
    with repo.connect(tmp_path / "t.db") as conn:
        repo.init_db(conn)
        run_id = repo.create_run(conn, _run())

        repo.add_span(
            conn,
            SpanRecord(None, run_id, "model", "think", 100, 50, "q", "a", 10, "ok", None),
        )
        repo.add_span(
            conn,
            SpanRecord(None, run_id, "tool", "read_file", 200, 20, "p", "c", 0, "ok", None),
        )
        spans = repo.list_spans(conn, run_id)
        assert [s.name for s in spans] == ["think", "read_file"]
        assert spans[0].start_offset_ms == 100

        repo.add_score(
            conn,
            EvalScoreRecord(None, run_id, "correctness", 0.9, "human", "u1", "t1", "good"),
        )
        repo.add_score(
            conn,
            EvalScoreRecord(None, run_id, "safety", 1.0, "rule", "sys", "t2", ""),
        )
        scores = repo.list_scores(conn, run_id)
        assert [s.dimension for s in scores] == ["correctness", "safety"]
        assert scores[0].score == 0.9


def test_delete_run_cascades(tmp_path: Path) -> None:
    with repo.connect(tmp_path / "t.db") as conn:
        repo.init_db(conn)
        run_id = repo.create_run(conn, _run())
        repo.add_span(
            conn, SpanRecord(None, run_id, "model", "think", 0, 10, "", "", 1, "ok", None)
        )
        repo.add_score(conn, EvalScoreRecord(None, run_id, "safety", 1.0, "rule", "", "t", ""))

        assert repo.delete_run(conn, run_id) is True
        assert repo.get_run(conn, run_id) is None
        assert repo.list_spans(conn, run_id) == []
        assert repo.list_scores(conn, run_id) == []
        assert repo.delete_run(conn, run_id) is False


def test_connect_rolls_back_on_error(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    try:
        with repo.connect(db) as conn:
            repo.init_db(conn)
            repo.create_run(conn, _run())
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass
    with repo.connect(db) as conn:
        assert repo.list_runs(conn) == []
