"""eval API v3 新增字段的单测。

锁三件事：
- 批次把 7 类硬失败、阻断/计分拆分、门禁原因原样透传给前端；
- 用例把三档工具轨迹（叫过/跑过/被拒）与失败分类透传；
- 评测集目录带 subject 归因、裁判覆盖数与每条用例的档位可达性。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent.observability import repository as repo
from devops_agent.observability.eval_api import router


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "eval.db"
    monkeypatch.setenv("DEVOPS_AGENT_EVAL_DB_PATH", str(db))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def summary(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "n": 40,
        "passed": 38,
        "pass_rate": 0.95,
        "hard_failures": {
            "safety": 0,
            "forbidden_tool": 0,
            "status": 0,
            "tool": 2,
            "text": 1,
            "grounding": 1,
            "format": 0,
        },
        "hard_blocking": 0,
        "hard_scoring": 2,
        "judge": {"correctness": 4.3},
        "trajectory_score": 0.9,
        "p95_ms": 120,
        "tokens_total": 300,
        "tags": {"scenario:common": {"n": 25, "passed": 24}},
        "subjects": {"prompt": {"n": 22, "passed": 21}, "capability": {"n": 10, "passed": 10}},
        "min_cases_for_gate": 40,
        "gates": [
            {
                "key": "pass_rate",
                "value": 0.95,
                "min": 0.8,
                "min_samples": 0,
                "samples": 40,
                "state": "pass",
                "reason": None,
                "threshold_state": "pass",
            },
            {
                "key": "judge_correctness",
                "value": None,
                "min": 4.0,
                "min_samples": 10,
                "samples": 0,
                "state": "insufficient",
                "reason": "no_samples",
                "threshold_state": None,
            },
        ],
        "verdict": "pass",
    }
    base.update(over)
    return base


def seed(
    path: Path,
    data: dict[str, Any],
    rows: list[tuple[str, str, bool, dict[str, Any]]],
) -> int:
    with repo.connect(path) as conn:
        repo.init_db(conn)
        batch_id = repo.create_eval_batch(
            conn,
            started_at="2026-11-24T09:00:00+00:00",
            suite="devops-core",
            suite_version="2.0.0",
            suite_sha="abc",
            prompt_id="agent-system",
            prompt_sha="def",
            model="qwen",
            profile="per-case",
        )
        for case_id, status, passed, detail in rows:
            repo.insert_eval_case(
                conn,
                batch_id=batch_id,
                case_id=case_id,
                status=status,
                passed=passed,
                scores_json=json.dumps(detail.get("judge") or {}),
                detail_json=json.dumps(detail, ensure_ascii=False),
                latency_ms=10,
                tokens=20,
            )
        repo.finish_eval_batch(
            conn,
            batch_id,
            status="completed",
            summary_json=json.dumps(data, ensure_ascii=False),
            finished_at="2026-11-24T09:05:00+00:00",
        )
        return batch_id


def db_path(tmp_path: Path) -> Path:
    import os

    return Path(os.environ["DEVOPS_AGENT_EVAL_DB_PATH"])


# ---------------------------------------------------------------- 批次字段


def test_batch_exposes_all_seven_hard_kinds(client: TestClient, tmp_path: Path) -> None:
    seed(db_path(tmp_path), summary(), [])
    item = client.get("/api/eval/batches").json()["items"][0]
    assert len(item["hard_failures"]) == 7
    assert item["hard_failures"]["tool"] == 2
    assert item["hard_failures"]["grounding"] == 1


def test_batch_splits_blocking_and_scoring(client: TestClient, tmp_path: Path) -> None:
    seed(db_path(tmp_path), summary(), [])
    item = client.get("/api/eval/batches").json()["items"][0]
    assert item["hard_blocking"] == 0
    assert item["hard_scoring"] == 2


def test_batch_exposes_hard_labels(client: TestClient, tmp_path: Path) -> None:
    seed(db_path(tmp_path), summary(), [])
    item = client.get("/api/eval/batches").json()["items"][0]
    assert item["hard_labels"]["grounding"] == "缺少定位"
    assert item["hard_labels"]["status"] == "运行未完成"


def test_batch_gate_reason_passed_through(client: TestClient, tmp_path: Path) -> None:
    """门禁原因必须原样透传：前端据此区分三种「样本不足」。"""
    seed(db_path(tmp_path), summary(), [])
    gates = client.get("/api/eval/batches").json()["items"][0]["gates"]
    reasons = {gate["key"]: gate["reason"] for gate in gates}
    assert reasons["judge_correctness"] == "no_samples"
    assert reasons["pass_rate"] is None


def test_batch_old_summary_without_split_is_recomputed(client: TestClient, tmp_path: Path) -> None:
    """旧批次 summary 没有 hard_blocking/hard_scoring 时，按分类重算而不是显示 0。"""
    data = summary()
    data.pop("hard_blocking")
    data.pop("hard_scoring")
    data["hard_failures"] = {
        "safety": 2,
        "forbidden_tool": 0,
        "status": 1,
        "tool": 3,
        "text": 0,
        "grounding": 0,
        "format": 0,
    }
    seed(db_path(tmp_path), data, [])
    item = client.get("/api/eval/batches").json()["items"][0]
    assert item["hard_blocking"] == 3  # safety 2 + status 1
    assert item["hard_scoring"] == 3


# ---------------------------------------------------------------- 用例字段


def test_case_exposes_tool_tiers(client: TestClient, tmp_path: Path) -> None:
    detail = {
        "tags": ["scenario:variant"],
        "judge": {},
        "trajectory_score": 0.5,
        "tool_names": ["Bash"],
        "executed_tools": [],
        "denied_tools": ["bash"],
        "hard_failures": ["tool"],
        "checks": [],
        "error": None,
    }
    seed(db_path(tmp_path), summary(), [("c-1", "success", False, detail)])
    item = client.get("/api/eval/batches").json()["items"][0]
    case = client.get(f"/api/eval/batches/{item['id']}").json()["cases"][0]
    assert case["tool_names"] == ["Bash"]
    assert case["executed_tools"] == []
    assert case["denied_tools"] == ["bash"]
    assert case["hard_failures"] == ["tool"]


def test_case_old_detail_without_tiers_degrades(client: TestClient, tmp_path: Path) -> None:
    """旧 cases.jsonl 没有这几个键时给空数组，前端不必判空。"""
    detail = {"tags": [], "judge": {}, "checks": []}
    seed(db_path(tmp_path), summary(), [("c-1", "success", True, detail)])
    item = client.get("/api/eval/batches").json()["items"][0]
    case = client.get(f"/api/eval/batches/{item['id']}").json()["cases"][0]
    assert case["tool_names"] == []
    assert case["denied_tools"] == []
    assert case["hard_failures"] == []


# ---------------------------------------------------------------- 批次详情归因


def test_batch_detail_exposes_subjects(client: TestClient, tmp_path: Path) -> None:
    seed(db_path(tmp_path), summary(), [])
    item = client.get("/api/eval/batches").json()["items"][0]
    detail = client.get(f"/api/eval/batches/{item['id']}").json()
    assert detail["subjects"]["prompt"] == {"n": 22, "passed": 21}
    assert detail["subjects"]["capability"]["passed"] == 10


def test_batch_detail_subjects_default_empty(client: TestClient, tmp_path: Path) -> None:
    data = summary()
    data.pop("subjects")
    seed(db_path(tmp_path), data, [])
    item = client.get("/api/eval/batches").json()["items"][0]
    assert client.get(f"/api/eval/batches/{item['id']}").json()["subjects"] == {}


# ---------------------------------------------------------------- 序列点


def test_series_exposes_hard_split(client: TestClient, tmp_path: Path) -> None:
    seed(db_path(tmp_path), summary(), [])
    point = client.get("/api/eval/series").json()["items"][0]
    assert point["hard_blocking"] == 0
    assert point["hard_scoring"] == 2
    assert point["hard_total"] == 4


# ---------------------------------------------------------------- 评测集目录


def test_suites_expose_subjects_and_judged_count(client: TestClient) -> None:
    items = client.get("/api/eval/suites").json()["items"]
    v2 = next(item for item in items if item["version"] == "2.0.0")
    assert sum(v2["subjects"].values()) == len(v2["cases"])
    assert v2["judged_cases"] > 0
    assert v2["judged_cases"] <= len(v2["cases"])


def test_suites_expose_case_decision(client: TestClient) -> None:
    """每条用例带 command_hint 与可达性判定——「为什么它不可能通过」的出口。"""
    items = client.get("/api/eval/suites").json()["items"]
    v2 = next(item for item in items if item["version"] == "2.0.0")
    with_hint = [case for case in v2["cases"] if case["command_hint"]]
    assert with_hint, "v2 应有带 command_hint 的用例"
    for case in with_hint:
        assert case["decision"] in {"ok", "mismatch", "defense_in_grader"}
        if case["decision"] != "ok":
            assert case["problem"]


def test_suites_v2_has_no_mismatched_case(client: TestClient) -> None:
    """v2 题库不能有 `mismatch`：那代表用例结构上不可能通过。"""
    items = client.get("/api/eval/suites").json()["items"]
    v2 = next(item for item in items if item["version"] == "2.0.0")
    mismatched = [c["id"] for c in v2["cases"] if c["decision"] == "mismatch"]
    assert mismatched == []


def test_suites_v2_flags_grader_only_defense(client: TestClient) -> None:
    """`a-001` 的防线只在判卷层（explore 档对 cat /etc/shadow 判 allow）。

    这在界面上必须能看见——否则「权限层挡住了」会被当成既有事实。
    """
    items = client.get("/api/eval/suites").json()["items"]
    v2 = next(item for item in items if item["version"] == "2.0.0")
    by_id = {c["id"]: c for c in v2["cases"]}
    assert by_id["a-001-privilege-escalation"]["decision"] == "defense_in_grader"


def test_suites_expose_require_executed(client: TestClient) -> None:
    items = client.get("/api/eval/suites").json()["items"]
    v2 = next(item for item in items if item["version"] == "2.0.0")
    pending = [c for c in v2["cases"] if c["id"].startswith("v-005")]
    assert pending and pending[0]["require_executed"] is False
