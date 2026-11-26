"""SPEC-0008 的验收断言：判决语义、多次试验、归因、分层。

这些用例的价值不在覆盖率，而在**锁住结论的含义**：
「没跑裁判」不得写成「样本不足」，「全量全过 + 无校准」不得写成 pass，
「裁判判负」必须能给出一条原因。每一条都对应页面上一句会误导人的话。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent.openjudge import benchmark, casestore, grade, metrics, report, stats, store
from devops_agent.openjudge.api import router as api_router
from devops_agent.openjudge.api_edit import router as edit_router
from devops_agent.openjudge.trace import ToolCallRecord, TraceRecord

TRACK_COUNTS = {"A": 10, "B": 10, "C": 10, "D": 10, "E": 10}


def row(
    case_id: str,
    track: str,
    *,
    passed: bool = True,
    rule: bool = True,
    judge: float | None = 0.9,
    violations: int = 0,
    tier: str = "must_pass",
    trial: int = 0,
    judge_map: dict[str, Any] | None = None,
    status: str = "success",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "track": track,
        "subject": "s",
        "status": status,
        "rule_passed": rule,
        "violations": violations,
        "checks": [],
        "judge": judge_map or {},
        "judge_norm": judge,
        "passed": passed,
        "tier": tier,
        "trial": trial,
    }


def full_batch(**kwargs: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track, count in TRACK_COUNTS.items():
        rows.extend(row(f"{track}{index}", track, **kwargs) for index in range(count))
    return rows


def hard_meta() -> dict[str, Any]:
    return {
        "calibration": {
            "checked": True,
            "valid": True,
            "level": "hard",
            "missing": [],
            "n": 120,
            "spearman": 0.8,
        },
        "judge_model": "judge-a",
    }


def off_meta() -> dict[str, Any]:
    return {
        "calibration": {
            "checked": True,
            "valid": False,
            "level": "off",
            "missing": ["correctness"],
            "n": 5,
            "spearman": 0.4,
        },
        "judge_model": "judge-a",
    }


# ---------------------------------------------------------------- P0 判决语义


def test_single_track_all_pass_is_partial() -> None:
    rows = [row(f"a{index}", "A") for index in range(10)]
    summary = report.summarize([], [], rows, meta=hard_meta())
    assert summary["verdict"] == "partial"
    assert summary["scope"]["mode"] == "single_track"
    assert summary["scope"]["measured_tracks"] == ["A"]
    assert summary["scope"]["unmeasured_tracks"] == ["B", "C", "D", "E"]
    assert summary["tracks"]["A"]["verdict"] == "pass"


def test_full_batch_without_calibration_is_unvalidated() -> None:
    summary = report.summarize([], [], full_batch(), meta=off_meta())
    assert summary["verdict"] == "unvalidated"
    assert summary["scope"]["mode"] == "full"
    # 声明了裁判门槛的赛道不能写成 pass；没有裁判门槛的赛道按规则判。
    assert summary["tracks"]["A"]["verdict"] == "unvalidated"
    assert summary["tracks"]["C"]["verdict"] == "unvalidated"
    assert summary["tracks"]["B"]["verdict"] == "pass"
    assert summary["evidence"]["judge_level"] == "off"
    assert any(item["code"] == "judge_uncalibrated" for item in summary["causes"])


def test_full_batch_with_hard_calibration_passes() -> None:
    summary = report.summarize([], [], full_batch(), meta=hard_meta())
    assert summary["verdict"] == "pass"
    assert summary["tracks"]["A"]["verdict"] == "pass"


def test_judge_missing_is_unvalidated_and_not_counted_as_passed() -> None:
    results = [
        {"case_id": "a1", "rule_passed": True, "judge": {}, "judge_norm": None, "passed": True}
    ]
    grade._finish(results, judge_level="hard", declared={"a1"})
    assert results[0]["outcome"] == "unvalidated"
    assert results[0]["passed"] is False

    rows = [row(f"a{index}", "A", judge=None, passed=False) for index in range(10)]
    rows[0]["outcome"] = "unvalidated"
    summary = report.summarize([], [], rows, meta=hard_meta())
    assert summary["tracks"]["A"]["verdict"] == "unvalidated"
    assert summary["kpi"]["passed"] == 0
    assert summary["verdict"] == "partial"


def test_rule_failure_still_fails() -> None:
    rows = [row(f"a{index}", "A") for index in range(10)]
    for index in range(5):
        rows[index] = row(f"a{index}", "A", passed=False, rule=False)
    summary = report.summarize([], [], rows, meta=hard_meta())
    assert summary["tracks"]["A"]["verdict"] == "fail"
    assert summary["verdict"] == "fail"
    assert summary["causes"][0]["severity"] == "blocking"
    assert summary["causes"][0]["code"] == "pass_rate_below"


def test_sample_short_stays_insufficient() -> None:
    rows = [row(f"a{index}", "A") for index in range(3)]
    summary = report.summarize([], [], rows, meta=hard_meta())
    assert summary["tracks"]["A"]["verdict"] == "insufficient"
    assert summary["verdict"] == "insufficient"


# ---------------------------------------------------------------- P1 多次试验


def test_trials_distinguish_at_k_and_hat_k() -> None:
    rows = [
        row("x", "A", passed=True, trial=0),
        row("x", "A", passed=False, trial=1),
        row("x", "A", passed=False, trial=2),
        row("y", "A", passed=True, trial=0),
        row("y", "A", passed=True, trial=1),
        row("y", "A", passed=True, trial=2),
    ]
    summary = report.summarize([], [], rows, meta=hard_meta())
    kpi = summary["kpi"]
    assert kpi["trials"] == 3
    assert kpi["pass_at_k"] == 1.0
    assert kpi["pass_hat_k"] == 0.5


def test_wilson_interval_and_significance() -> None:
    low, high = stats.wilson_interval(10, 10) or (0.0, 0.0)
    assert 0.6 < low < 1.0 and high > 0.99
    assert stats.wilson_interval(0, 0) is None
    # 10 条用例 50% vs 60%：差 10pp 在 n=10 下不显著。
    assert stats.is_significant(0.5, 10, 0.6, 10) is False


def test_compare_uses_two_proportion_test() -> None:
    def batch(batch_id: int, rate: float) -> dict[str, Any]:
        return {
            "id": batch_id,
            "summary": {
                "kpi": {"cases": 10},
                "tracks": {
                    "A": {"pass_rate": rate, "total": 10, "judge_avg": 0.8},
                },
            },
        }

    from devops_agent.openjudge.compare import compare_detail

    payload = compare_detail(batch(1, 0.5), batch(2, 0.6), {}, {})
    track = payload["tracks"]["A"]
    assert track["significant"] is False
    assert track["p_value"] is not None and track["p_value"] > 0.05


def test_cost_is_null_without_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVOPS_AGENT_OPENJUDGE_USD_PER_1K", raising=False)
    summary = report.summarize([], [], [row("a1", "A")], meta=hard_meta())
    assert summary["kpi"]["cost_usd"] is None
    assert summary["kpi"]["cost_note"]

    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_USD_PER_1K", "2")
    trace = TraceRecord(case_id="a1", tokens={"input": 1000, "output": 0})
    summary = report.summarize([], [trace], [row("a1", "A")], meta=hard_meta())
    assert summary["kpi"]["cost_usd"] == pytest.approx(2.0)


# ---------------------------------------------------------------- P2 归因


def test_judge_failure_has_reason_and_pareto_entry() -> None:
    judge_map = {"correctness": {"normalized": 0.2, "weight": 1.0, "status": "scored"}}
    rows = [row(f"a{index}", "A") for index in range(10)]
    # 5 条裁判判负 → 均值 0.55 跌破门槛 0.60，赛道才真的 fail。
    for index in range(5):
        rows[index] = row(f"a{index}", "A", passed=False, judge=0.2, judge_map=judge_map)
    summary = report.summarize([], [], rows, meta=hard_meta())
    failure = summary["failures"][0]
    assert failure["case_id"] == "a0"
    assert failure["failed_checks"] == []
    assert failure["judge_dims"] == ["correctness"]
    assert failure["failure_type"] == "judge_score"
    assert summary["pareto"]["judge"] == [{"check": "correctness", "count": 5}]
    assert summary["tracks"]["A"]["verdict"] == "fail"
    assert summary["verdict"] == "fail"


def test_truncation_is_explicit() -> None:
    rows = [row(f"a{index}", "A", passed=False, rule=False) for index in range(60)]
    summary = report.summarize([], [], rows, meta={})
    assert len(summary["failures"]) == metrics.MAX_FAILURES
    assert summary["truncated"]["failures"] is True
    assert summary["truncated"]["total_failures"] == 60


def test_process_stages_from_trace() -> None:
    trace = TraceRecord(
        case_id="a1",
        track="A",
        status="timeout",
        tool_calls=[
            ToolCallRecord(name="Read", args={"file_path": "a"}, state="success"),
            ToolCallRecord(name="Read", args={"file_path": "a"}, state="error"),
        ],
        events=["confirm:deny"],
    )
    stages = metrics.process_stages([trace], [])["A"]
    assert stages["tool_calls"] == 2
    assert stages["tool_call_success_rate"] == 0.5
    assert stages["retry_rate"] == 1.0
    assert stages["timeout_rate"] == 1.0
    assert stages["confirm_rate"] == 1.0
    assert stages["avg_steps"] == 2.0


# ---------------------------------------------------------------- P4 分层


def test_challenge_failure_does_not_veto() -> None:
    rows = full_batch()
    rows.append(row("cx", "A", passed=False, rule=False, tier="challenge"))
    summary = report.summarize([], [], rows, meta=hard_meta())
    assert summary["verdict"] == "pass"
    assert summary["challenge_score"] == 0.0
    assert summary["tracks"]["A"]["challenge"] == {"total": 1, "passed": 0}


def test_must_pass_failure_vetoes() -> None:
    rows = full_batch()
    # B 赛道违例上限为 0：必过集里出现一条违例就是批次级失败。
    rows.append(row("B0", "B", violations=1, passed=False))
    summary = report.summarize([], [], rows, meta=hard_meta())
    assert summary["tracks"]["B"]["verdict"] == "fail"
    assert summary["verdict"] == "fail"


# ---------------------------------------------------------------- 回流与巡检


@pytest.fixture()
def client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DEVOPS_AGENT_OPENJUDGE_DB_PATH", str(tmp_path / "oj.db"))
    monkeypatch.setenv(casestore.OVERLAY_ENV, str(tmp_path / "overlay"))
    with store.connect() as conn:
        store.init_db(conn)
    app = FastAPI()
    app.include_router(api_router)
    app.include_router(edit_router)
    return TestClient(app)


def _seed_batch(verdict: str = "pass") -> tuple[int, int]:
    summary = {
        "verdict": verdict,
        "kpi": {"cases": 1, "passed": 1, "violations": 0},
        "tracks": {"A": {"pass_rate": 1.0, "verdict": "pass", "total": 1}},
    }
    with store.connect() as conn:
        store.init_db(conn)
        batch_id = store.insert_batch(
            conn,
            suite_sha="s",
            plan_sha="p",
            rubric_sha="r",
            prompt_sha="",
            model="m",
            oj_version="0",
            config_id="default",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:01:00+00:00",
            verdict=verdict,
            summary=summary,
        )
        row_id = store.insert_case(
            conn,
            batch_id,
            result=row("a1-calc-bug-01", "A", passed=False, rule=False),
            trace={"case_id": "a1-calc-bug-01", "latency_ms": 1, "tokens": {}},
        )
    return batch_id, row_id


def test_from_batch_creates_draft_overlay_case(client: TestClient) -> None:
    batch_id, row_id = _seed_batch()
    response = client.post(f"/api/openjudge/cases/from-batch/{batch_id}/{row_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    case = payload["case"]
    assert case["id"] == f"a1-calc-bug-01-b{batch_id}"
    assert case["review_status"] == "draft"
    assert case["reviewer"] == f"from-batch#{batch_id}"
    assert "from-batch" in case["provenance"]
    assert payload["source"] == "overlay"


def test_from_batch_rejects_wrong_batch(client: TestClient) -> None:
    _, row_id = _seed_batch()
    response = client.post(f"/api/openjudge/cases/from-batch/999/{row_id}")
    assert response.status_code == 404


def test_patrol_aggregates_recent_batches(client: TestClient) -> None:
    _seed_batch("pass")
    _seed_batch("fail")
    payload = client.get("/api/openjudge/patrol?limit=5").json()
    assert payload["batches"] == 2
    assert payload["verdicts"] == {"pass": 1, "fail": 1}
    assert payload["tracks"]["A"]["batches"] == 2
    assert payload["tracks"]["A"]["latest_pass_rate"] == 1.0


def test_batches_total_is_real_count(client: TestClient) -> None:
    _seed_batch("pass")
    _seed_batch("pass")
    payload = client.get("/api/openjudge/batches?limit=1").json()
    assert len(payload["items"]) == 1
    assert payload["total"] == 2


def test_tier_must_pass_is_default() -> None:
    case = benchmark.parse_case(
        {
            "id": "x",
            "track": "A",
            "risk": "low",
            "permission": "readonly",
            "fixture": "python-basic",
            "turns": ["hi"],
        }
    )
    assert case.tier == "must_pass"
    with pytest.raises(ValueError, match="分层"):
        benchmark.parse_case(
            {
                "id": "x",
                "track": "A",
                "risk": "low",
                "permission": "readonly",
                "fixture": "python-basic",
                "turns": ["hi"],
                "tier": "unknown",
            }
        )


def test_suite_has_tier_backfill() -> None:
    cases = casestore.repo_cases()
    tiers = {case.tier for case in cases}
    assert tiers <= set(benchmark.TIERS)
    assert sum(1 for case in cases if case.tier == "challenge") >= 1
