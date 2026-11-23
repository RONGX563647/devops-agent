"""评测只读 API 单测：空库、有数据、筛选与对比（临时 DB，不联网）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devops_agent.main import app
from devops_agent.observability import repository as repo

client = TestClient(app)


def summary(
    *,
    n: int = 2,
    passed: int = 2,
    pass_rate: float = 1.0,
    correctness: float = 4.5,
    verdict: str = "pass",
) -> dict[str, Any]:
    return {
        "n": n,
        "passed": passed,
        "pass_rate": pass_rate,
        "hard_failures": {"safety": 0, "forbidden_tool": 0, "format": 0},
        "judge": {"correctness": correctness},
        "trajectory_score": 0.9,
        "p95_ms": 120,
        "tokens_total": 300,
        "tags": {"scenario:common": {"n": n, "passed": passed}},
        "min_cases_for_gate": 1,
        "verdict": verdict,
    }


def seed_batch(
    path: Path,
    *,
    started_at: str,
    summary_data: dict[str, Any],
    case_rows: list[tuple[str, str, bool, dict[str, Any]]],
) -> int:
    with repo.connect(path) as conn:
        repo.init_db(conn)
        batch_id = repo.create_eval_batch(
            conn,
            started_at=started_at,
            suite="devops-core",
            suite_version="1.0.0",
            suite_sha="abc",
            prompt_id="default",
            prompt_sha="def",
            model="qwen",
            profile="readonly",
        )
        for case_id, status, passed, detail in case_rows:
            repo.insert_eval_case(
                conn,
                batch_id=batch_id,
                case_id=case_id,
                status=status,
                passed=passed,
                scores_json=json.dumps(detail.get("judge") or {}),
                detail_json=json.dumps(detail, ensure_ascii=False),
                latency_ms=int(detail.get("latency_ms", 10)),
                tokens=int(detail.get("tokens", 20)),
            )
        repo.finish_eval_batch(
            conn,
            batch_id,
            status="completed",
            summary_json=json.dumps(summary_data, ensure_ascii=False),
            finished_at=started_at,
        )
        return batch_id


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "eval.db"
    monkeypatch.setenv(repo.EVAL_DB_PATH_ENV, str(path))
    return path


def test_empty_database_returns_empty_and_404(db: Path) -> None:
    resp = client.get("/api/eval/batches")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}
    assert client.get("/api/eval/batches/1").status_code == 404
    assert client.get("/api/eval/batches/1/cases").status_code == 404
    assert client.get("/api/eval/compare", params={"base": 1, "head": 2}).status_code == 404


def test_unavailable_database_does_not_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 把 DB 路径指向一个目录：sqlite 无法打开，API 必须降级而不是 500
    monkeypatch.setenv(repo.EVAL_DB_PATH_ENV, str(tmp_path))
    resp = client.get("/api/eval/batches")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert client.get("/api/eval/batches/1").status_code == 404
    compared = client.get("/api/eval/compare", params={"base": 1, "head": 2})
    assert compared.status_code == 200
    assert compared.json()["verdict"] == "insufficient"


def test_list_and_detail_with_data(db: Path) -> None:
    common_detail = {
        "tags": ["scenario:common"],
        "trajectory_score": 1.0,
        "judge": {"correctness": 5.0},
        "checks": [{"name": "status", "ok": True, "detail": "ok"}],
        "latency_ms": 100,
        "tokens": 50,
    }
    failed_detail = {
        "tags": ["scenario:adversarial"],
        "trajectory_score": 0.2,
        "judge": None,
        "checks": [{"name": "forbidden_tool", "ok": False, "detail": "Write"}],
        "latency_ms": 200,
        "tokens": 80,
    }
    seed_batch(
        db,
        started_at="2026-11-24T09:00:00+00:00",
        summary_data=summary(),
        case_rows=[
            ("c-1", "success", True, common_detail),
            ("a-1", "success", False, failed_detail),
        ],
    )
    listed = client.get("/api/eval/batches").json()
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["suite"] == "devops-core"
    assert item["prompt_hash"] == "def"
    assert item["n"] == 2
    assert item["pass_rate"] == 1.0
    assert item["hard_failures"] == {"safety": 0, "forbidden_tool": 0, "format": 0}
    assert item["judge"] == {"correctness": 4.5}
    assert item["trajectory_score"] == 0.9
    assert item["verdict"] == "pass"
    assert item["baseline_id"] is None

    detail = client.get(f"/api/eval/batches/{item['id']}").json()
    assert detail["batch"]["id"] == item["id"]
    assert detail["tags"] == {"scenario:common": {"n": 2, "passed": 2}}
    assert len(detail["cases"]) == 2
    first = detail["cases"][0]
    assert first["case_id"] == "c-1"
    assert first["tags"] == ["scenario:common"]
    assert first["judge"] == {"correctness": 5.0}
    assert first["checks"][0]["name"] == "status"


def test_case_filters_and_pagination(db: Path) -> None:
    details = [
        {
            "tags": ["scenario:common"],
            "trajectory_score": 1.0,
            "judge": None,
            "checks": [],
            "latency_ms": 10,
            "tokens": 5,
        },
        {
            "tags": ["scenario:adversarial"],
            "trajectory_score": 0.4,
            "judge": None,
            "checks": [],
            "latency_ms": 20,
            "tokens": 6,
        },
    ]
    batch_id = seed_batch(
        db,
        started_at="2026-11-24T09:00:00+00:00",
        summary_data=summary(),
        case_rows=[
            ("c-1", "success", True, details[0]),
            ("a-1", "error", False, details[1]),
        ],
    )
    success = client.get(f"/api/eval/batches/{batch_id}/cases", params={"status": "success"})
    assert success.json()["total"] == 1
    assert success.json()["items"][0]["case_id"] == "c-1"

    tagged = client.get(
        f"/api/eval/batches/{batch_id}/cases", params={"tag": "scenario:adversarial"}
    )
    assert tagged.json()["total"] == 1
    assert tagged.json()["items"][0]["case_id"] == "a-1"

    paged = client.get(f"/api/eval/batches/{batch_id}/cases", params={"limit": 1, "offset": 1})
    assert paged.json()["total"] == 2
    assert [item["case_id"] for item in paged.json()["items"]] == ["a-1"]


def test_compare_two_batches(db: Path) -> None:
    base_id = seed_batch(
        db,
        started_at="2026-11-24T09:00:00+00:00",
        summary_data=summary(n=2, passed=2, pass_rate=1.0, correctness=4.5),
        case_rows=[
            (
                "c-1",
                "success",
                True,
                {"tags": ["scenario:common"], "checks": [], "latency_ms": 10, "tokens": 5},
            )
        ],
    )
    head_id = seed_batch(
        db,
        started_at="2026-11-24T10:00:00+00:00",
        summary_data=summary(n=2, passed=1, pass_rate=0.5, correctness=4.0, verdict="warn"),
        case_rows=[
            (
                "c-1",
                "success",
                True,
                {"tags": ["scenario:common"], "checks": [], "latency_ms": 10, "tokens": 5},
            ),
            (
                "c-2",
                "error",
                False,
                {"tags": ["scenario:edge"], "checks": [], "latency_ms": 20, "tokens": 7},
            ),
        ],
    )
    compared = client.get("/api/eval/compare", params={"base": base_id, "head": head_id})
    assert compared.status_code == 200
    body = compared.json()
    assert body["verdict"] == "warn"
    metrics = {item["key"]: item for item in body["metrics"]}
    assert metrics["pass_rate"]["base"] == 1.0
    assert metrics["pass_rate"]["head"] == 0.5
    assert metrics["pass_rate"]["better"] == "higher"
    assert {item["case_id"] for item in body["cases"]} == {"c-1", "c-2"}


def test_compare_missing_batch_returns_404(db: Path) -> None:
    batch_id = seed_batch(
        db,
        started_at="2026-11-24T09:00:00+00:00",
        summary_data=summary(),
        case_rows=[],
    )
    assert (
        client.get("/api/eval/compare", params={"base": batch_id, "head": 999}).status_code == 404
    )


def test_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "eval.db"
    with repo.connect(path) as conn:
        repo.init_db(conn)
        repo.init_db(conn)
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"eval_batches", "eval_cases"} <= tables
    with pytest.raises(sqlite3.Error):
        # 路径是目录时 connect 失败，验证 API 测试里的降级前提成立
        sqlite3.connect(str(tmp_path))
