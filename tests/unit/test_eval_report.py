"""报告聚合、基线与渲染单测。"""

from __future__ import annotations

import pytest

from devops_agent.eval.dataset import EvalCase
from devops_agent.eval.graders import CaseScore, TraceResult
from devops_agent.eval.report import aggregate, compare, render_markdown
from devops_agent.eval.runner import CaseRun


def make_run(
    case_id: str,
    *,
    passed: bool = True,
    hard: list[str] | None = None,
    trajectory: float | None = None,
    judge: dict[str, float] | None = None,
    latency_ms: int = 10,
    tokens: int = 5,
    tags: list[str] | None = None,
) -> CaseRun:
    case = EvalCase(
        id=case_id,
        tags=tags if tags is not None else ["scenario:common"],
        input="x",
        expected={"must_contain": ["ok"]},
    )
    score = CaseScore(
        passed=passed,
        checks=[],
        trajectory_score=trajectory,
        hard_failures=hard or [],
    )
    trace = TraceResult(
        status="success" if passed else "error",
        final_text="ok",
        latency_ms=latency_ms,
        tokens=tokens,
    )
    return CaseRun(
        case=case,
        score=score,
        trace=trace,
        judge_scores=judge,
        latency_ms=latency_ms,
        tokens=tokens,
    )


def test_aggregate_collects_metrics() -> None:
    runs = [
        make_run("a", judge={"correctness": 4.0, "clarity": 3.0}, trajectory=0.9, latency_ms=100),
        make_run(
            "b",
            passed=False,
            hard=["safety", "forbidden_tool"],
            judge={"correctness": 3.0},
            trajectory=0.5,
            latency_ms=300,
            tags=["scenario:adversarial"],
        ),
    ]
    summary = aggregate(runs, min_cases_for_gate=1)
    assert summary["n"] == 2
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["hard_failures"] == {"safety": 1, "forbidden_tool": 1, "format": 0}
    assert summary["judge"] == {"correctness": 3.5, "clarity": 3.0}
    assert summary["trajectory_score"] == 0.7
    assert summary["p95_ms"] == 300
    assert summary["tokens_total"] == 10
    assert summary["tags"]["scenario:common"] == {"n": 1, "passed": 1}
    assert summary["verdict"] == "fail"


def test_aggregate_verdict_insufficient_and_pass() -> None:
    runs = [make_run("a")]
    assert aggregate(runs, min_cases_for_gate=2)["verdict"] == "insufficient"
    assert aggregate(runs, min_cases_for_gate=1)["verdict"] == "pass"


def test_aggregate_text_hard_failure_still_fails() -> None:
    runs = [make_run("a", passed=False, hard=["text"])]
    summary = aggregate(runs, min_cases_for_gate=1)
    assert summary["hard_failures"] == {"safety": 0, "forbidden_tool": 0, "format": 0}
    assert summary["verdict"] == "fail"


def test_compare_metrics_and_case_diff() -> None:
    baseline = {
        "n": 2,
        "passed": 2,
        "pass_rate": 1.0,
        "judge": {"correctness": 4.5},
        "trajectory_score": 0.9,
        "p95_ms": 100,
        "tokens_total": 100,
        "hard_failures": {"safety": 0, "forbidden_tool": 0, "format": 0},
        "min_cases_for_gate": 1,
        "cases": [{"case_id": "a", "passed": True}, {"case_id": "b", "passed": True}],
    }
    head = {
        "n": 2,
        "passed": 1,
        "pass_rate": 0.8,
        "judge": {"correctness": 4.2},
        "trajectory_score": 0.88,
        "p95_ms": 120,
        "tokens_total": 90,
        "hard_failures": {"safety": 0, "forbidden_tool": 0, "format": 0},
        "min_cases_for_gate": 1,
        "cases": [{"case_id": "a", "passed": True}, {"case_id": "b", "passed": False}],
    }
    compared = compare(head, baseline)
    assert compared["verdict"] == "warn"
    metrics = {item["key"]: item for item in compared["metrics"]}
    assert metrics["pass_rate"]["delta"] == pytest.approx(-0.2)
    assert metrics["pass_rate"]["better"] == "higher"
    assert metrics["p95_ms"]["better"] == "lower"
    assert metrics["tokens_total"]["better"] == "lower"
    assert metrics["correctness"]["delta"] == pytest.approx(-0.3)
    assert compared["cases"] == [
        {"case_id": "a", "base_passed": True, "head_passed": True},
        {"case_id": "b", "base_passed": True, "head_passed": False},
    ]


def test_compare_hard_failure_and_insufficient() -> None:
    hard = {
        "n": 2,
        "pass_rate": 1.0,
        "hard_failures": {"safety": 1, "forbidden_tool": 0, "format": 0},
        "min_cases_for_gate": 1,
    }
    assert compare(hard, hard)["verdict"] == "fail"

    small = {"n": 1, "pass_rate": 1.0, "hard_failures": {}, "min_cases_for_gate": 40}
    assert compare(small, small)["verdict"] == "insufficient"


def test_compare_without_cases_field() -> None:
    compared = compare({"n": 1, "pass_rate": 1.0}, {"n": 1, "pass_rate": 1.0})
    assert compared["cases"] == []
    assert compared["metrics"]


def test_render_markdown_contains_sections() -> None:
    summary = aggregate([make_run("a", judge={"correctness": 4.0})], min_cases_for_gate=1)
    text = render_markdown(summary)
    assert "# 评测报告" in text
    assert "## 硬失败" in text
    assert "## Judge 均值" in text
    assert "## 标签分布" in text
    assert "correctness" in text


def test_render_markdown_for_compare() -> None:
    compared = compare({"n": 1, "pass_rate": 1.0}, {"n": 1, "pass_rate": 0.5})
    text = render_markdown(compared)
    assert "## 基线对比" in text
    assert "pass_rate" in text
