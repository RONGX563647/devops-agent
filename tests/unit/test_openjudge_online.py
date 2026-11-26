"""在线评测：运行槽、进度落库、任务生命周期与 HTTP 契约。

重点不是「能跑通」，而是几件不该发生的事：
- 两个任务同时占用运行槽（内存锁在多 worker 下会漏）；
- 任务重启后永久卡在 running，把槽位占死；
- 取消之后状态又变回 done；
- 覆盖基线悄悄下跌而 validate 仍然全绿。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent.openjudge import benchmark, casestore, run, run_store, service, store
from devops_agent.openjudge.api import router


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "oj.db"
    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_DB_PATH", str(target))
    with store.connect() as conn:
        store.init_db(conn)
    return target


def _claim(**kwargs: Any) -> int | None:
    with store.connect() as conn:
        return run_store.claim_run(
            conn,
            track=kwargs.get("track", "A"),
            total=kwargs.get("total", 3),
            judge=kwargs.get("judge", False),
            concurrency=kwargs.get("concurrency", 1),
        )


def test_second_claim_is_rejected_while_one_is_running(db: Path) -> None:
    """同一时刻只能有一个任务占用运行槽。"""
    first = _claim()
    assert first is not None
    assert _claim() is None


def test_finish_frees_the_slot_and_records_batch(db: Path) -> None:
    run_id = _claim(total=4)
    assert run_id is not None
    with store.connect() as conn:
        run_store.finish_run(conn, run_id, status="done", batch_id=9)
        row = run_store.get_run(conn, run_id)
    assert row is not None
    assert row["status"] == "done"
    assert row["batch_id"] == 9
    assert row["done"] == 4
    assert _claim() is not None


def test_run_is_marked_failed_after_restart(db: Path) -> None:
    """进程重启后残留的 running 不能永久占住槽位。"""
    run_id = _claim()
    assert run_id is not None
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with store.connect() as conn:
        conn.execute("UPDATE oj_runs SET updated_at = ? WHERE id = ?", (stale, run_id))
        conn.commit()
        assert run_store.reap_stale_runs(conn, max_age_seconds=600) == 1
        row = run_store.get_run(conn, run_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "interrupted"
    assert _claim() is not None


def test_progress_is_persisted_per_case(db: Path) -> None:
    run_id = _claim(total=3)
    assert run_id is not None
    with store.connect() as conn:
        run_store.update_progress(conn, run_id, 2)
        row = run_store.get_run(conn, run_id)
    assert row is not None
    assert row["done"] == 2


def _seed_batch(tracks: dict[str, Any], *, verdict: str = "pass", finished_at: str = "") -> int:
    summary = {
        "verdict": verdict,
        "kpi": {"cases": 2, "passed": 1, "pass_rate": 0.5, "judge_n": 0, "violations": 0},
        "tracks": tracks,
    }
    with store.connect() as conn:
        return store.insert_batch(
            conn,
            suite_sha="suite",
            plan_sha="plan",
            rubric_sha="",
            prompt_sha="",
            model="m",
            oj_version="0.2.5",
            config_id="",
            started_at=finished_at or datetime.now(UTC).isoformat(),
            finished_at=finished_at or datetime.now(UTC).isoformat(),
            verdict=verdict,
            summary=summary,
        )


def test_series_returns_points_in_ascending_order(db: Path) -> None:
    now = datetime.now(UTC)
    _seed_batch({"A": {"pass_rate": 0.5}}, finished_at=(now - timedelta(days=2)).isoformat())
    _seed_batch({"A": {"pass_rate": 0.9}}, finished_at=now.isoformat())
    with store.connect() as conn:
        points = store.series(conn, days=7)
    assert [round(point["pass_rate"], 2) for point in points] == [0.5, 0.5]
    assert points[0]["batch_id"] < points[1]["batch_id"]


def test_series_respects_the_window(db: Path) -> None:
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    _seed_batch({"A": {"pass_rate": 0.1}}, finished_at=old)
    with store.connect() as conn:
        assert store.series(conn, days=7) == []


def test_latest_track_stats_skips_batches_without_the_track(db: Path) -> None:
    _seed_batch({"A": {"pass_rate": 0.8, "verdict": "pass"}})
    _seed_batch({"B": {"pass_rate": 0.2, "verdict": "fail"}})
    with store.connect() as conn:
        latest = store.latest_track_stats(conn, "A")
    assert latest is not None
    assert latest["pass_rate"] == 0.8


def test_latest_track_stats_is_none_without_data(db: Path) -> None:
    with store.connect() as conn:
        assert store.latest_track_stats(conn, "A") is None


# --------------------------------------------------------------------------- case_ids


def test_load_cases_by_ids_keeps_order_and_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    monkeypatch.setattr(casestore, "load_cases", lambda: fake_cases)
    picked = casestore.load_cases_by_ids(["a-02", "a-01", "a-02"])
    assert [case.id for case in picked] == ["a-02", "a-01"]
    with pytest.raises(ValueError) as exc:
        casestore.load_cases_by_ids(["a-01", "nope", "missing"])
    assert "nope" in str(exc.value) and "missing" in str(exc.value)


def test_service_case_ids_start_records_actual_total(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    monkeypatch.setenv(service.ENV_FLAG, "1")
    seen: list[list[str]] = []

    def load_by_ids(ids: list[str]) -> list[Any]:
        seen.append(ids)
        return [case for case in fake_cases if case.id in ids]

    async def no_execute(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(casestore, "load_cases_by_ids", load_by_ids)
    monkeypatch.setattr(service, "_execute", no_execute)
    payload = asyncio.run(
        service.start(
            track="B", limit=99, case_ids=["a-02", "a-01", "a-02"], judge=False, concurrency=1
        )
    )
    assert payload["total"] == 2
    assert seen == [["a-02", "a-01"]]
    with store.connect() as conn:
        row = run_store.get_run(conn, payload["run_id"])
    assert row is not None
    assert row["track"] == "" and row["total"] == 2


def test_service_case_ids_unknown_and_limit(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    monkeypatch.setenv(service.ENV_FLAG, "1")

    def unknown(ids: list[str]) -> list[Any]:
        raise ValueError(f"未知用例 id：{ids[0]}")

    monkeypatch.setattr(casestore, "load_cases_by_ids", unknown)
    with pytest.raises(ValueError, match="未知用例 id"):
        asyncio.run(service.start(track="", limit=1, case_ids=["nope"]))

    calls: list[list[str]] = []
    monkeypatch.setattr(
        casestore,
        "load_cases_by_ids",
        lambda ids: calls.append(ids) or fake_cases,
    )
    with pytest.raises(ValueError, match="最多 30 条"):
        asyncio.run(service.start(track="", limit=1, case_ids=[f"a-{i}" for i in range(31)]))
    assert calls == []


def test_compare_coverage_flags_regressions() -> None:
    current = {"tracks": {"A": 26}, "tools": {"Bash": 10}, "subjects": {}, "risks": {}}
    baseline = {"tracks": {"A": 28}, "tools": {"Bash": 10}, "subjects": {}, "risks": {}}
    problems = benchmark.compare_coverage(current, baseline)
    assert len(problems) == 1
    assert "tracks.A" in problems[0]


def test_compare_coverage_allows_growth() -> None:
    current = {"tracks": {"A": 30}, "tools": {"Bash": 11}, "subjects": {}, "risks": {}}
    baseline = {"tracks": {"A": 28}, "tools": {"Bash": 10}, "subjects": {}, "risks": {}}
    assert benchmark.compare_coverage(current, baseline) == []


# --------------------------------------------------------------------------- service


def test_online_flag_is_required(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    monkeypatch.delenv(service.ENV_FLAG, raising=False)
    monkeypatch.setattr(service, "load_cases", lambda track="", limit=0: fake_cases)
    with pytest.raises(RuntimeError):
        asyncio.run(service.start(track="A", limit=1, judge=False, concurrency=1))


def test_select_cases_limit_zero_loads_all(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    seen: dict[str, Any] = {}

    def fake_load(track: str = "", limit: int = 0) -> list[Any]:
        seen.update(track=track, limit=limit)
        return fake_cases

    monkeypatch.setattr(service, "load_cases", fake_load)
    cases, active_track = service._select_cases("", 0, None)
    assert seen == {"track": "", "limit": 0}
    assert cases == fake_cases
    assert active_track == ""


def test_unknown_track_is_rejected(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(service.ENV_FLAG, "1")
    with pytest.raises(ValueError):
        asyncio.run(service.start(track="Z", limit=1, judge=False, concurrency=1))


def test_busy_start_returns_the_active_run(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    monkeypatch.setenv(service.ENV_FLAG, "1")
    monkeypatch.setattr(service, "load_cases", lambda track="", limit=0: fake_cases)
    occupied = _claim()
    assert occupied is not None
    payload = asyncio.run(service.start(track="A", limit=1, judge=False, concurrency=1))
    assert payload["busy"] == occupied


def test_execute_marks_done_and_links_batch(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    run_id = _claim(total=len(fake_cases))
    assert run_id is not None

    async def fake_execute(*_: Any, **__: Any) -> run.RunResult:
        return run.RunResult(batch_id=11, summary={"verdict": "pass"}, out_dir=Path("."))

    monkeypatch.setattr(run, "execute", fake_execute)
    asyncio.run(service._execute(run_id, fake_cases, judge=False, concurrency=1))
    with store.connect() as conn:
        row = run_store.get_run(conn, run_id)
    assert row is not None
    assert row["status"] == "done"
    assert row["batch_id"] == 11


def test_execute_records_failure_instead_of_raising(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    run_id = _claim(total=len(fake_cases))
    assert run_id is not None

    async def boom(*_: Any, **__: Any) -> run.RunResult:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(run, "execute", boom)
    asyncio.run(service._execute(run_id, fake_cases, judge=False, concurrency=1))
    with store.connect() as conn:
        row = run_store.get_run(conn, run_id)
    assert row is not None
    assert row["status"] == "failed"
    assert "OperationalError" in row["error"]


def test_cancel_stops_between_cases(
    db: Path, monkeypatch: pytest.MonkeyPatch, fake_cases: list[Any]
) -> None:
    run_id = _claim(total=2)
    assert run_id is not None

    async def cancelled(*_: Any, should_stop: Any = None, **__: Any) -> run.RunResult:
        service._cancelled.add(run_id)
        assert should_stop is not None and should_stop()
        raise run.RunCancelled

    monkeypatch.setattr(run, "execute", cancelled)
    asyncio.run(service._execute(run_id, fake_cases, judge=False, concurrency=1))
    with store.connect() as conn:
        row = run_store.get_run(conn, run_id)
    assert row is not None
    assert row["status"] == "cancelled"
    assert run_id not in service._cancelled


# --------------------------------------------------------------------------- API


@pytest.fixture()
def client(db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv(service.ENV_FLAG, raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_post_run_requires_the_flag(client: TestClient) -> None:
    response = client.post("/api/openjudge/runs", json={"track": "A", "limit": 3})
    assert response.status_code == 400
    assert "DEVOPS_AGENT_OPENJUDGE_ONLINE" in response.json()["detail"]


def test_post_run_returns_202(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_start(**_: Any) -> dict[str, Any]:
        return {"run_id": 4, "total": 3}

    monkeypatch.setattr(service, "start", fake_start)
    response = client.post("/api/openjudge/runs", json={"track": "A", "limit": 3})
    assert response.status_code == 202
    assert response.json() == {"run_id": 4, "total": 3}


def test_post_run_accepts_limit_zero_for_all_cases(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_start(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"run_id": 6, "total": 90}

    monkeypatch.setattr(service, "start", fake_start)
    response = client.post("/api/openjudge/runs", json={"track": "", "limit": 0})
    assert response.status_code == 202
    assert seen["limit"] == 0
    assert seen["track"] == ""
    assert response.json()["total"] == 90


def test_post_run_rejects_track_and_case_ids(client: TestClient) -> None:
    response = client.post("/api/openjudge/runs", json={"track": "A", "case_ids": ["a-01"]})
    assert response.status_code == 400
    assert "二选一" in response.json()["detail"]


def test_post_run_forwards_case_ids(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_start(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"run_id": 5, "total": 2}

    monkeypatch.setattr(service, "start", fake_start)
    response = client.post("/api/openjudge/runs", json={"case_ids": ["a-02", "a-01"]})
    assert response.status_code == 202
    assert seen["case_ids"] == ["a-02", "a-01"]
    assert seen["track"] == ""


def test_post_run_conflict_exposes_active_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_start(**_: Any) -> dict[str, Any]:
        return {"busy": 7, "total": 0}

    monkeypatch.setattr(service, "start", fake_start)
    response = client.post("/api/openjudge/runs", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["run_id"] == 7


def test_run_detail_404(client: TestClient) -> None:
    assert client.get("/api/openjudge/runs/999").status_code == 404


def test_cancel_404(client: TestClient) -> None:
    assert client.post("/api/openjudge/runs/999/cancel").status_code == 404


def test_suites_exposes_server_side_thresholds(client: TestClient) -> None:
    payload = client.get("/api/openjudge/suites").json()
    tracks = {item["track"]: item for item in payload["tracks"]}
    assert set(tracks) == set(benchmark.TRACKS)
    assert tracks["A"]["rules"]["pass_rate"] == 0.6
    assert tracks["A"]["cases"] >= 1
    assert tracks["A"]["latest"] is None


def test_series_is_empty_without_batches(client: TestClient) -> None:
    assert client.get("/api/openjudge/series").json() == {"points": []}


@pytest.fixture()
def fake_cases() -> list[Any]:
    """只提供生命周期测试需要的两个字段，不加载真实题库。"""
    return [_Stub("a-01", "A"), _Stub("a-02", "A")]


class _Stub:
    def __init__(self, case_id: str, track: str) -> None:
        self.id = case_id
        self.track = track
