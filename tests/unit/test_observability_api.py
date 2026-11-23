"""观测 API 端到端单测（TestClient + tmp_path 隔离 DB）。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent.config import clear_cache
from devops_agent.main import app
from devops_agent.observability import repository as repo
from devops_agent.observability.models import RunRecord, SpanRecord

client = TestClient(app)


@pytest.fixture
def db(monkeypatch: object, tmp_path: Path) -> Path:
    """把 DB 指到临时目录，灌入两条 run 与若干 span。"""
    path = tmp_path / "obs.db"
    monkeypatch.setenv(repo.DB_PATH_ENV, str(path))  # type: ignore[attr-defined]
    clear_cache()
    with repo.connect(path) as conn:
        repo.init_db(conn)
        r1 = RunRecord(
            id=None,
            session_id="s1",
            domain="debug",
            status="success",
            input="定位 bug",
            output="定位到 app.py:10",
            started_at="2026-11-23T14:00:00+00:00",
            ended_at="2026-11-23T14:00:02+00:00",
            duration_ms=2000,
            model="qwen",
            total_tokens=150,
            health_score=88.0,
            input_tokens=100,
            output_tokens=50,
        )
        r2 = RunRecord(
            id=None,
            session_id="s2",
            domain="delivery",
            status="failed",
            input="部署",
            output="",
            started_at="2026-11-24T09:00:00+00:00",
            ended_at="2026-11-24T09:00:05+00:00",
            duration_ms=5000,
            model="qwen",
            total_tokens=80,
            health_score=40.0,
            input_tokens=60,
            output_tokens=20,
        )
        id1 = repo.create_run(conn, r1)
        repo.create_run(conn, r2)
        repo.add_span(
            conn,
            SpanRecord(
                id=None,
                run_id=id1,
                type="model",
                name="qwen",
                start_offset_ms=0,
                duration_ms=1800,
                input="",
                output="",
                tokens=150,
                status="ok",
                error=None,
            ),
        )
    yield path
    clear_cache()


def test_list_runs_and_filters(db: Path) -> None:
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["session_id"] == "s2"  # 倒序

    filtered = client.get("/api/runs", params={"domain": "debug"})
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["domain"] == "debug"

    paged = client.get("/api/runs", params={"limit": 1, "offset": 1})
    assert len(paged.json()["items"]) == 1
    assert paged.json()["total"] == 2


def test_get_run_and_spans(db: Path) -> None:
    detail = client.get("/api/runs/1")
    assert detail.status_code == 200
    assert detail.json()["session_id"] == "s1"
    assert client.get("/api/runs/999").status_code == 404

    spans = client.get("/api/runs/1/spans")
    assert spans.status_code == 200
    assert len(spans.json()) == 1
    assert spans.json()[0]["type"] == "model"
    assert client.get("/api/runs/999/spans").status_code == 404


def test_run_health(db: Path) -> None:
    resp = client.get("/api/runs/1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == 1
    assert 0.0 <= body["score"] <= 100.0
    assert set(body["dimensions"]) == {"execution", "efficiency", "quality", "safety"}
    assert client.get("/api/runs/999/health").status_code == 404


def test_metrics_summary(db: Path) -> None:
    resp = client.get("/api/metrics/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_runs"] == 2
    assert body["success_count"] == 1
    assert body["failed_count"] == 1
    assert body["success_rate"] == 0.5
    assert body["avg_health_score"] == 64.0
    assert body["p95_duration_ms"] == 5000
    assert body["token_proxy"] == 230
    assert body["tool_call_count"] == 0


def test_metrics_timeseries(db: Path) -> None:
    resp = client.get("/api/metrics/timeseries", params={"days": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["date"] == "2026-11-23"
    assert body[0]["success"] == 1
    assert body[1]["date"] == "2026-11-24"
    assert body[1]["failed"] == 1


def test_feedback_and_trends(db: Path) -> None:
    resp = client.post("/api/feedback/1", json={"score": 1.0, "comment": "👍"})
    assert resp.status_code == 200
    assert resp.json()["run_id"] == 1

    bad = client.post("/api/feedback/1", json={"score": 2.0})
    assert bad.status_code == 422
    missing = client.post("/api/feedback/999", json={"score": 1.0})
    assert missing.status_code == 404

    trends = client.get("/api/eval/trends")
    assert trends.status_code == 200
    body = trends.json()
    assert body["count"] == 1
    assert body["averages"]["correctness"] == 1.0
    assert body["by_source"]["human"] == 1


def test_feedback_rejects_unknown_dimension(db: Path) -> None:
    """非法评测维度应在入口被拒（422），不污染 DB。"""
    bad = client.post("/api/feedback/1", json={"score": 1.0, "dimension": "not_a_dimension"})
    assert bad.status_code == 422
    trends = client.get("/api/eval/trends").json()
    assert trends["count"] == 0
