"""OpenJudge 只读 API 的单测：空库、批次/用例、对比、rubric 与覆盖。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent.openjudge import store
from devops_agent.openjudge.api import router


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_DB_PATH", str(tmp_path / "oj.db"))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def seed_batch(*, verdict: str = "pass", tracks: dict[str, Any] | None = None) -> int:
    """写入一个批次并返回 id；用例由调用方补。"""
    summary = {
        "verdict": verdict,
        "kpi": {"cases": 2, "passed": 2, "judge_n": 1, "violations": 0},
        "tracks": tracks or {"A": {"n": 2, "passed": 2, "pass_rate": 1.0, "judge_avg": 0.9}},
    }
    with store.connect() as conn:
        store.init_db(conn)
        return store.insert_batch(
            conn,
            suite_sha="suite",
            plan_sha="plan",
            rubric_sha="rubric",
            prompt_sha="prompt",
            model="deepseek",
            oj_version="0.2.5",
            config_id="default",
            started_at="2026-11-25T00:00:00Z",
            finished_at="2026-11-25T00:05:00Z",
            verdict=verdict,
            summary=summary,
        )


def seed_case(batch_id: int, *, case_id: str, passed: bool, track: str = "A") -> int:
    result = {
        "case_id": case_id,
        "track": track,
        "risk": "low",
        "subject": "repo_qa",
        "status": "success",
        "passed": passed,
        "checks": [{"name": "must_cite", "kind": "fact", "passed": passed, "security": False}],
        "judge": {"correctness": {"score": 4.0, "normalized": 0.75}},
    }
    trace = {"latency_ms": 100, "tokens": {"input": 3, "output": 2}, "events": ["DONE"]}
    with store.connect() as conn:
        store.init_db(conn)
        return store.insert_case(conn, batch_id, result=result, trace=trace)


def test_health_without_data_is_explicit(client: TestClient) -> None:
    body = client.get("/api/openjudge/health").json()
    assert body["has_data"] is False
    assert body["latest_batch"] is None


def test_batches_and_detail(client: TestClient) -> None:
    batch_id = seed_batch()
    seed_case(batch_id, case_id="a-1", passed=True)

    listed = client.get("/api/openjudge/batches").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == batch_id
    assert listed["items"][0]["verdict"] == "pass"

    detail = client.get(f"/api/openjudge/batches/{batch_id}").json()
    assert detail["summary"]["kpi"]["cases"] == 2

    assert client.get("/api/openjudge/batches/9999").status_code == 404


def test_cases_filters_and_404(client: TestClient) -> None:
    batch_id = seed_batch()
    seed_case(batch_id, case_id="a-ok", passed=True)
    seed_case(batch_id, case_id="a-bad", passed=False)

    payload = client.get(f"/api/openjudge/batches/{batch_id}/cases").json()
    assert payload["total"] == 2

    failed = client.get(
        f"/api/openjudge/batches/{batch_id}/cases", params={"passed": "false"}
    ).json()
    assert failed["total"] == 1
    assert failed["items"][0]["case_id"] == "a-bad"

    row_id = failed["items"][0]["id"]
    case = client.get(f"/api/openjudge/cases/{row_id}").json()
    assert case["case_id"] == "a-bad"
    assert case["checks"][0]["name"] == "must_cite"

    assert client.get("/api/openjudge/cases/9999").status_code == 404


def test_compare_flags_and_flips(client: TestClient) -> None:
    base = seed_batch(verdict="fail")
    head = seed_batch(verdict="pass")
    seed_case(base, case_id="a-1", passed=False)
    seed_case(head, case_id="a-1", passed=True)

    payload = client.get("/api/openjudge/compare", params={"base": base, "head": head}).json()
    assert payload["comparable"] is True
    assert payload["tracks"]["A"]["delta"] == 0.0
    assert payload["flips"] == [{"case_id": "a-1", "base": False, "head": True}]

    assert (
        client.get("/api/openjudge/compare", params={"base": base, "head": 9999}).status_code == 404
    )


def test_rubrics_list(client: TestClient) -> None:
    with store.connect() as conn:
        store.init_db(conn)
        store.insert_rubric(
            conn,
            name="repo_qa_quality",
            version="v1",
            sha="abc",
            status="draft",
            reviewer="",
        )
    payload = client.get("/api/openjudge/rubrics").json()
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "repo_qa_quality"


def test_coverage_reads_repo_cases(client: TestClient) -> None:
    payload = client.get("/api/openjudge/coverage").json()
    # 仓库自带题库：A28/B24/C14/D12/E12
    assert payload["cases"] == 90
    assert payload["suite_sha"]
    assert payload["coverage"]["tracks"]["A"] == 28
