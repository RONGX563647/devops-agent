"""报告聚合、基线与渲染单测。"""

from __future__ import annotations

import pytest

from devops_agent.eval.dataset import EvalCase
from devops_agent.eval.graders import CaseScore, TraceResult
from devops_agent.eval.report import (
    aggregate,
    compare,
    compatibility,
    minimal_detectable_effect,
    render_markdown,
)
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
    # 7 类全部出现，缺的补 0（报告要能显示每一类的次数）
    assert summary["hard_failures"] == {
        "safety": 1,
        "forbidden_tool": 1,
        "status": 0,
        "tool": 0,
        "text": 0,
        "grounding": 0,
        "format": 0,
    }
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


def test_scoring_kind_is_counted_and_visible() -> None:
    """`text` 属于计分类：要出现在硬失败表里，且不单独否决。"""
    runs = [make_run("a", passed=False, hard=["text"])]
    summary = aggregate(runs, min_cases_for_gate=1)
    assert summary["hard_failures"]["text"] == 1
    assert summary["hard_blocking"] == 0
    assert summary["hard_scoring"] == 1
    # pass_rate 0.0 < 阈值 0.8，所以仍判 fail —— 但失败来源是阈值，不是「任意硬失败」
    assert summary["verdict"] == "fail"


def test_scoring_kind_does_not_veto_when_pass_rate_meets_threshold() -> None:
    """一条用例事实缺失、其余全过 -> pass_rate 0.9333 > 0.8，不得无条件 fail。

    这是旧实现的核心缺陷：`pass_rate.min` 永不生效，实测被这条场景判成 fail。
    """
    runs = [make_run(f"c{i}") for i in range(14)]
    runs.append(make_run("bad", passed=False, hard=["text"]))
    summary = aggregate(runs, min_cases_for_gate=1)
    assert summary["pass_rate"] > 0.8
    assert summary["hard_scoring"] == 1
    assert summary["hard_blocking"] == 0
    assert summary["verdict"] == "pass"


def test_blocking_kind_vetoes_even_with_high_pass_rate() -> None:
    """安全越界必须一票否决，不受 pass_rate 影响。"""
    runs = [make_run(f"c{i}") for i in range(14)]
    runs.append(make_run("leak", passed=False, hard=["safety"]))
    summary = aggregate(runs, min_cases_for_gate=1)
    assert summary["pass_rate"] > 0.8
    assert summary["hard_blocking"] == 1
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


def test_gates_mark_insufficient_below_min_cases() -> None:
    """样本不足是「不能判」，不是「未达标」。"""
    runs = [make_run(f"c{index}") for index in range(3)]
    summary = aggregate(runs, min_cases_for_gate=40, thresholds={"pass_rate": {"min": 0.8}})

    assert summary["gates"][0]["state"] == "insufficient"
    assert summary["verdict"] == "insufficient"


def test_gates_fail_below_threshold() -> None:
    runs = [make_run("a", passed=True), make_run("b", passed=False)]
    summary = aggregate(runs, min_cases_for_gate=2, thresholds={"pass_rate": {"min": 0.8}})

    assert summary["gates"][0]["state"] == "fail"
    assert summary["verdict"] == "fail"


def test_gate_sample_count_ignores_missing_judge() -> None:
    """judge 只有 1 条样本时，正确性门禁应判 insufficient 而不是不达标。"""
    runs = [make_run("a", judge={"correctness": 3.0}), make_run("b")]
    summary = aggregate(
        runs,
        min_cases_for_gate=2,
        thresholds={"judge_correctness": {"min": 4.0, "min_samples": 2}},
    )

    gate = next(item for item in summary["gates"] if item["key"] == "judge_correctness")
    assert gate["samples"] == 1
    assert gate["state"] == "insufficient"


def test_mde_uses_smaller_sample() -> None:
    assert minimal_detectable_effect(10, 40) == 0.1
    assert minimal_detectable_effect(0, 40) == 1.0


def test_compatibility_blocks_when_judge_model_changes() -> None:
    shared = {
        "suite_sha": "s",
        "grader_version": "v2",
        "prompt_sha": "p",
        "model": "m",
        "profile": "r",
        "n": 4,
        "min_cases_for_gate": 1,
        "verdict": "pass",
    }
    base = {**shared, "judge_model": "j1"}
    head = {**shared, "judge_model": "j2"}

    compat = compatibility(head, base)
    assert compat["level"] == "blocking"
    assert "judge_model" in compat["changed"]


def test_compatibility_is_confounded_when_prompt_and_model_change() -> None:
    shared = {
        "suite_sha": "s",
        "grader_version": "v2",
        "profile": "r",
        "judge_model": "j",
        "n": 4,
        "min_cases_for_gate": 1,
        "verdict": "pass",
    }
    base = {**shared, "prompt_sha": "p1", "model": "m1"}
    head = {**shared, "prompt_sha": "p2", "model": "m2"}

    assert compatibility(head, base)["level"] == "confounded"


def test_compare_returns_not_comparable_when_suite_changed() -> None:
    """题库变了就不是同一个实验，delta 不能当成结论。"""
    shared = {
        "n": 4,
        "passed": 4,
        "pass_rate": 1.0,
        "hard_failures": {},
        "min_cases_for_gate": 1,
        "verdict": "pass",
        "grader_version": "v2",
        "prompt_sha": "p",
        "model": "m",
        "profile": "r",
        "judge_model": "j",
    }
    head = {**shared, "suite_sha": "s-new"}
    base = {**shared, "suite_sha": "s-old"}

    compared = compare(head, base)
    assert compared["verdict"] == "not_comparable"
    assert compared["compatibility"]["level"] == "blocking"


def test_compare_only_prompt_changed_is_clean() -> None:
    shared = {
        "n": 4,
        "passed": 4,
        "pass_rate": 1.0,
        "hard_failures": {},
        "min_cases_for_gate": 1,
        "verdict": "pass",
        "suite_sha": "s",
        "grader_version": "v2",
        "model": "m",
        "profile": "r",
        "judge_model": "j",
    }
    head = {**shared, "prompt_sha": "p2"}
    base = {**shared, "prompt_sha": "p1"}

    compared = compare(head, base)
    assert compared["compatibility"]["level"] == "clean"
    assert compared["compatibility"]["changed"] == ["prompt_sha"]
    assert compared["verdict"] == "pass"


def test_render_markdown_shows_gates_and_precision() -> None:
    runs = [make_run("a", passed=True), make_run("b", passed=False)]
    summary = aggregate(runs, min_cases_for_gate=2, thresholds={"pass_rate": {"min": 0.8}})
    text = render_markdown(summary)

    assert "## 门禁" in text
    assert "未达" in text
