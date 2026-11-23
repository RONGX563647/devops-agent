"""评测报告 —— 聚合、基线对比与 Markdown 渲染。

报告是纯函数：输入 CaseRun 列表或 summary dict，输出 JSON 友好结构。
所有字段名都与 `/api/eval` 的响应契约对齐，CLI 与 API 共用同一套口径。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import ceil
from typing import Any

from devops_agent.eval.judge import JUDGE_DIMENSIONS
from devops_agent.eval.runner import CaseRun

_HARD_KINDS: tuple[str, ...] = ("safety", "forbidden_tool", "format")
# 指标口径：True 表示越大越好
_METRIC_KEYS: tuple[tuple[str, bool], ...] = (
    ("pass_rate", True),
    ("correctness", True),
    ("trajectory", True),
    ("p95_ms", False),
    ("tokens_total", False),
)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _p95(values: Sequence[int]) -> int:
    """最近秩法 p95：样本不足时取最大值。"""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, ceil(len(ordered) * 0.95) - 1)
    return int(ordered[index])


def _judge_means(runs: Iterable[CaseRun]) -> dict[str, float] | None:
    samples: dict[str, list[float]] = {dimension: [] for dimension in JUDGE_DIMENSIONS}
    for run in runs:
        if not run.judge_scores:
            continue
        for dimension, value in run.judge_scores.items():
            if dimension in samples:
                samples[dimension].append(value)
    means: dict[str, float] = {}
    for dimension, values in samples.items():
        mean = _mean(values)
        if mean is not None:
            means[dimension] = round(mean, 4)
    return means or None


def aggregate(runs: Iterable[CaseRun], min_cases_for_gate: int = 40) -> dict[str, Any]:
    """把一组用例运行聚合成 summary_json。"""
    items = list(runs)
    n = len(items)
    passed = sum(1 for run in items if run.score.passed)
    hard = {kind: 0 for kind in _HARD_KINDS}
    for run in items:
        for kind in set(run.score.hard_failures):
            if kind in hard:
                hard[kind] += 1

    tags: dict[str, dict[str, int]] = {}
    for run in items:
        for tag in run.case.tags:
            bucket = tags.setdefault(tag, {"n": 0, "passed": 0})
            bucket["n"] += 1
            bucket["passed"] += 1 if run.score.passed else 0

    trajectories = [
        run.score.trajectory_score for run in items if run.score.trajectory_score is not None
    ]
    trajectory = _mean(trajectories)

    if any(run.score.hard_failures for run in items):
        verdict = "fail"
    elif n < min_cases_for_gate:
        verdict = "insufficient"
    else:
        verdict = "pass"

    return {
        "n": n,
        "passed": passed,
        "pass_rate": round(passed / n, 4) if n else 0.0,
        "hard_failures": hard,
        "judge": _judge_means(items),
        "trajectory_score": round(trajectory, 4) if trajectory is not None else None,
        "p95_ms": _p95([run.latency_ms for run in items]),
        "tokens_total": sum(run.tokens for run in items),
        "tags": tags,
        "min_cases_for_gate": min_cases_for_gate,
        "verdict": verdict,
        "cases": [
            {"case_id": run.case.id, "passed": run.score.passed, "status": run.trace.status}
            for run in items
        ],
    }


def _metric_value(summary: dict[str, Any], key: str) -> float | None:
    """从 summary 里取指标；judge 维度嵌套在 judge 对象里。"""
    if key == "correctness":
        judge = summary.get("judge")
        value = judge.get("correctness") if isinstance(judge, dict) else None
    elif key == "trajectory":
        value = summary.get("trajectory_score")
    else:
        value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _case_map(summary: dict[str, Any]) -> dict[str, bool]:
    cases = summary.get("cases")
    if not isinstance(cases, list):
        return {}
    mapping: dict[str, bool] = {}
    for item in cases:
        if isinstance(item, dict) and isinstance(item.get("case_id"), str):
            mapping[str(item["case_id"])] = bool(item.get("passed"))
    return mapping


def compare(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """当前报告 vs 基线：指标 delta、用例变化与结论。"""
    metrics: list[dict[str, Any]] = []
    deltas: dict[str, float | None] = {}
    for key, higher_is_better in _METRIC_KEYS:
        base_value = _metric_value(baseline, key)
        head_value = _metric_value(report, key)
        delta = (
            head_value - base_value if base_value is not None and head_value is not None else None
        )
        deltas[key] = delta
        metrics.append(
            {
                "key": key,
                "base": base_value,
                "head": head_value,
                "delta": round(delta, 4) if delta is not None else None,
                # 方向而不是「这次是好是坏」：前端据此决定 delta 的着色方向
                "better": "higher" if higher_is_better else "lower",
            }
        )

    base_cases = _case_map(baseline)
    head_cases = _case_map(report)
    cases = [
        {
            "case_id": case_id,
            "base_passed": base_cases.get(case_id),
            "head_passed": head_cases.get(case_id),
        }
        for case_id in sorted(set(base_cases) | set(head_cases))
    ]

    hard = report.get("hard_failures")
    has_hard_failure = (
        isinstance(hard, dict) and any(int(value or 0) > 0 for value in hard.values())
    ) or report.get("verdict") == "fail"
    n = int(report.get("n", 0) or 0)
    min_cases = int(report.get("min_cases_for_gate", 40) or 40)

    pass_delta = deltas.get("pass_rate")
    correctness_delta = deltas.get("correctness")
    if has_hard_failure:
        verdict = "fail"
    elif n < min_cases:
        verdict = "insufficient"
    elif (pass_delta is not None and pass_delta < -0.02) or (
        correctness_delta is not None and correctness_delta < -0.2
    ):
        verdict = "warn"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "metrics": metrics,
        "cases": cases,
        "n": n,
        "pass_rate": report.get("pass_rate"),
        "baseline_pass_rate": baseline.get("pass_rate"),
        "hard_failures": hard if isinstance(hard, dict) else {},
    }


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def render_markdown(summary: dict[str, Any]) -> str:
    """把聚合报告（或对比报告）渲染成可提交到 PR 的 Markdown。"""
    lines = ["# 评测报告", ""]
    verdict = summary.get("verdict", "-")
    lines.append(f"- 结论：**{verdict}**")
    if "n" in summary:
        lines.append(
            f"- 用例：{summary.get('passed', 0)}/{summary.get('n', 0)} 通过"
            f"（pass_rate {_fmt(summary.get('pass_rate'))}）"
        )
    lines.append("")

    metrics = summary.get("metrics")
    if isinstance(metrics, list):
        lines.extend(
            [
                "## 基线对比",
                "",
                "| 指标 | 基线 | 当前 | Δ | 更好 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in metrics:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"| {item.get('key')} | {_fmt(item.get('base'))} | {_fmt(item.get('head'))} "
                f"| {_fmt(item.get('delta'))} | {_fmt(item.get('better'))} |"
            )
        lines.append("")

    hard = summary.get("hard_failures")
    if isinstance(hard, dict):
        lines.extend(["## 硬失败", "", "| 分类 | 次数 |", "| --- | --- |"])
        for key in _HARD_KINDS:
            lines.append(f"| {key} | {hard.get(key, 0)} |")
        lines.append("")

    judge = summary.get("judge")
    if isinstance(judge, dict) and judge:
        lines.extend(["## Judge 均值", "", "| 维度 | 分数 |", "| --- | --- |"])
        for dimension in JUDGE_DIMENSIONS:
            if dimension in judge:
                lines.append(f"| {dimension} | {_fmt(judge[dimension])} |")
        lines.append("")

    tags = summary.get("tags")
    if isinstance(tags, dict) and tags:
        lines.extend(["## 标签分布", "", "| 标签 | 用例 | 通过 |", "| --- | --- | --- |"])
        for tag, bucket in sorted(tags.items()):
            if isinstance(bucket, dict):
                lines.append(f"| {tag} | {bucket.get('n', 0)} | {bucket.get('passed', 0)} |")
        lines.append("")

    lines.append(
        f"- 轨迹分：{_fmt(summary.get('trajectory_score'))}；"
        f"p95：{_fmt(summary.get('p95_ms'))} ms；"
        f"tokens：{_fmt(summary.get('tokens_total'))}"
    )
    return "\n".join(lines).rstrip() + "\n"
