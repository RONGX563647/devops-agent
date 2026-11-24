"""硬失败分类单一事实源、判决分层、门禁成因的单测。

这些用例锁的都是实证过的缺陷：
- 7 类硬失败只渲染 3 类，导致 `text`/`tool`/`grounding`/`status` 在报告里隐身；
- 「任意硬失败即 fail」让 `thresholds.pass_rate` 永不生效（0.9333 也判 fail）；
- 三种 `insufficient` 成因在报告里长得一模一样。
"""

from __future__ import annotations

import pytest

from devops_agent.eval.dataset import EvalCase
from devops_agent.eval.graders import CaseScore, TraceResult, grade
from devops_agent.eval.kinds import (
    BLOCKING_KEYS,
    HARD_KEYS,
    SCORING_KEYS,
    labels,
    zero_counts,
)
from devops_agent.eval.report import _gate_rows, aggregate, render_markdown
from devops_agent.eval.runner import CaseRun

THRESHOLDS = {
    "pass_rate": {"min": 0.8, "min_samples": 0},
    "judge_correctness": {"min": 4.0, "min_samples": 10},
    "trajectory": {"min": 0.85, "min_samples": 10},
}


def make_run(case_id: str, *, passed: bool = True, hard: list[str] | None = None) -> CaseRun:
    case = EvalCase(
        id=case_id,
        tags=["scenario:common"],
        input="x",
        expected={"must_contain": ["ok"]},
    )
    score = CaseScore(
        passed=passed,
        checks=[],
        trajectory_score=0.9 if passed else 0.4,
        hard_failures=hard or [],
    )
    return CaseRun(
        case=case,
        score=score,
        trace=TraceResult(status="success" if passed else "error", final_text="ok"),
        judge_scores=None,
        latency_ms=10,
        tokens=5,
    )


# --------------------------------------------------------------- kinds 单一源


def test_kinds_cover_all_seven() -> None:
    assert len(HARD_KEYS) == 7
    assert set(zero_counts()) == set(HARD_KEYS)


def test_kinds_partition_into_blocking_and_scoring() -> None:
    """严重度必须二分且无交集：判决就靠这个划分。"""
    assert BLOCKING_KEYS.isdisjoint(SCORING_KEYS)
    assert BLOCKING_KEYS | SCORING_KEYS == set(HARD_KEYS)
    assert BLOCKING_KEYS == {"safety", "forbidden_tool", "status"}


def test_kinds_have_chinese_labels() -> None:
    table = labels()
    assert table["safety"] == "安全越界"
    assert table["text"] == "事实缺失"
    assert table["grounding"] == "缺少定位"


# ------------------------------------------------------- grader 拒绝未知分类


def test_grader_rejects_unknown_hard_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """拼错的分类名必须在判卷时炸掉，而不是被报告层静默吞成 0。"""
    from devops_agent.eval import graders

    case = EvalCase(
        id="x",
        tags=["scenario:common"],
        input="q",
        expected={"must_contain": ["never-appears"]},
    )
    trace = TraceResult(status="success", final_text="没有那个词")

    # 正常路径先确认：缺必含词 -> text
    assert "text" in grade(case, trace).hard_failures

    original = graders._record

    def broken(hard: list[str], *kinds: str) -> None:
        original(hard, "totally-bogus")

    monkeypatch.setattr(graders, "_record", broken)
    with pytest.raises(ValueError, match="未知硬失败分类"):
        graders.grade(case, trace)


# ------------------------------------------------------------- 报告渲染可见性


def test_render_shows_every_hard_kind() -> None:
    """报告必须列出全部 7 类：漏一类就等于该类失败在报告里隐身。"""
    runs = [
        make_run("ok"),
        make_run("a", passed=False, hard=["text"]),
        make_run("b", passed=False, hard=["tool"]),
        make_run("c", passed=False, hard=["grounding"]),
        make_run("d", passed=False, hard=["format"]),
        make_run("e", passed=False, hard=["status"]),
    ]
    summary = aggregate(runs, min_cases_for_gate=1, thresholds=THRESHOLDS)
    md = render_markdown(summary)
    hard_section = md.split("## 硬失败")[1].split("\n## ")[0]
    for key in HARD_KEYS:
        assert key in hard_section, f"{key} 未出现在硬失败表里"
    # 每类的实际次数也要能看到
    assert summary["hard_failures"]["text"] == 1
    assert summary["hard_failures"]["tool"] == 1
    assert summary["hard_failures"]["grounding"] == 1


def test_render_marks_severity() -> None:
    summary = aggregate([make_run("ok")], min_cases_for_gate=1, thresholds=THRESHOLDS)
    md = render_markdown(summary)
    hard_section = md.split("## 硬失败")[1].split("\n## ")[0]
    assert "阻断" in hard_section
    assert "计分" in hard_section


# --------------------------------------------------------------- 判定分层实证


def test_scoring_failure_keeps_pass_when_rate_above_threshold() -> None:
    """14 过 1 个 text 失败 -> pass_rate 0.9333 > 0.8 -> pass。

    旧实现会判 fail，等于 `thresholds.pass_rate` 从未生效。
    """
    runs = [make_run(f"c{i}") for i in range(14)]
    runs.append(make_run("bad", passed=False, hard=["text"]))
    summary = aggregate(runs, min_cases_for_gate=1, thresholds=THRESHOLDS)
    assert summary["pass_rate"] == pytest.approx(0.9333, abs=1e-4)
    assert summary["verdict"] == "pass"


def test_blocking_failure_vetoes_high_pass_rate() -> None:
    runs = [make_run(f"c{i}") for i in range(14)]
    runs.append(make_run("leak", passed=False, hard=["safety"]))
    summary = aggregate(runs, min_cases_for_gate=1, thresholds=THRESHOLDS)
    assert summary["pass_rate"] == pytest.approx(0.9333, abs=1e-4)
    assert summary["verdict"] == "fail"


def test_pass_rate_threshold_actually_decides() -> None:
    """过少的通过数要能被阈值判死 —— 这是「阈值真的在起作用」的下界证明。"""
    runs = [make_run(f"c{i}") for i in range(5)]
    runs.extend(make_run(f"bad{i}", passed=False, hard=["text"]) for i in range(5))
    summary = aggregate(runs, min_cases_for_gate=1, thresholds=THRESHOLDS)
    assert summary["pass_rate"] == 0.5
    gate = next(g for g in summary["gates"] if g["key"] == "pass_rate")
    assert gate["state"] == "fail"
    assert summary["verdict"] == "fail"


# ------------------------------------------------------- insufficient 三态成因


def test_gate_reason_distinguishes_three_causes() -> None:
    """三种「不能判」必须给出不同的 reason，否则处置方式无从判断。"""
    below_cases = _gate_rows(
        passed=40,
        n=15,
        judge_means={"correctness": 4.7},
        judge_counts={"correctness": 12},
        trajectory=0.9,
        trajectory_n=15,
        thresholds=THRESHOLDS,
        min_cases_for_gate=40,
    )
    no_samples = _gate_rows(
        passed=40,
        n=40,
        judge_means=None,
        judge_counts={},
        trajectory=0.9,
        trajectory_n=40,
        thresholds=THRESHOLDS,
        min_cases_for_gate=40,
    )
    few_samples = _gate_rows(
        passed=40,
        n=40,
        judge_means={"correctness": 4.7},
        judge_counts={"correctness": 3},
        trajectory=0.9,
        trajectory_n=40,
        thresholds=THRESHOLDS,
        min_cases_for_gate=40,
    )
    pick = lambda rows: next(g for g in rows if g["key"] == "judge_correctness")  # noqa: E731
    assert pick(below_cases)["reason"] == "n_below_gate"
    assert pick(no_samples)["reason"] == "no_samples"
    assert pick(few_samples)["reason"] == "samples_below_min"
    # 三者 state 相同（都是不能判），靠 reason 区分
    assert pick(below_cases)["state"] == pick(no_samples)["state"] == "insufficient"


def test_gate_threshold_state_is_independent_of_sample_state() -> None:
    """`state` 说「能不能判」，`threshold_state` 说「阈值过没过」，两者独立。

    小样本下 pass_rate=0.0 仍然是明确的坏结果：不能因为 n < 门禁量就只剩
    「样本不足」。否则一次全灭的 nightly 会退 0 码（绿灯）。
    """
    rows = _gate_rows(
        passed=0,
        n=15,
        judge_means=None,
        judge_counts={},
        trajectory=0.1,
        trajectory_n=15,
        thresholds=THRESHOLDS,
        min_cases_for_gate=40,
    )
    pass_rate = next(g for g in rows if g["key"] == "pass_rate")
    assert pass_rate["state"] == "insufficient"
    assert pass_rate["reason"] == "n_below_gate"
    assert pass_rate["threshold_state"] == "fail"


def test_small_sample_all_green_stays_insufficient() -> None:
    """全绿的小样本不能被误报成 fail。"""
    runs = [make_run(f"c{i}") for i in range(15)]
    summary = aggregate(runs, min_cases_for_gate=40, thresholds=THRESHOLDS)
    assert summary["pass_rate"] == 1.0
    assert summary["verdict"] == "insufficient"


def test_small_sample_all_red_is_fail_not_insufficient() -> None:
    """全灭的小样本必须判 fail：0 通过是明确的坏结果。"""
    runs = [make_run(f"c{i}", passed=False, hard=["text"]) for i in range(15)]
    summary = aggregate(runs, min_cases_for_gate=40, thresholds=THRESHOLDS)
    assert summary["pass_rate"] == 0.0
    assert summary["verdict"] == "fail"


def test_gate_reason_is_none_when_decidable() -> None:
    rows = _gate_rows(
        passed=40,
        n=40,
        judge_means={"correctness": 4.6},
        judge_counts={"correctness": 12},
        trajectory=0.9,
        trajectory_n=40,
        thresholds=THRESHOLDS,
        min_cases_for_gate=40,
    )
    pick = lambda rows: next(g for g in rows if g["key"] == "judge_correctness")  # noqa: E731
    assert pick(rows)["state"] == "pass"
    assert pick(rows)["reason"] is None


def test_render_gate_reason_text() -> None:
    rows = _gate_rows(
        passed=40,
        n=40,
        judge_means=None,
        judge_counts={},
        trajectory=0.9,
        trajectory_n=40,
        thresholds=THRESHOLDS,
        min_cases_for_gate=40,
    )
    md = render_markdown({"verdict": "pass", "n": 40, "gates": rows, "hard_failures": {}})
    assert "裁判未运行" in md


# ------------------------------------------------------- subjects 归因出口


def test_render_subjects_section() -> None:
    """按 subject 归因必须出现在人读报告里；此前只存在于 JSON。"""
    summary = aggregate(
        [make_run("a"), make_run("b"), make_run("c")],
        min_cases_for_gate=1,
        thresholds=THRESHOLDS,
    )
    assert summary["subjects"] == {"capability": {"n": 3, "passed": 3}}
    md = render_markdown(summary)
    assert "## 归因分布" in md
    assert "capability" in md
