"""评测报告 —— 聚合、基线对比与 Markdown 渲染。

报告是纯函数：输入 CaseRun 列表或 summary dict，输出 JSON 友好结构。
所有字段名都与 `/api/eval` 的响应契约对齐，CLI 与 API 共用同一套口径。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
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


def _count_hard(items: Sequence[CaseRun]) -> dict[str, int]:
    """逐分类统计硬失败用例数（同类只算一次）。"""
    hard = {kind: 0 for kind in _HARD_KINDS}
    for run in items:
        for kind in set(run.score.hard_failures):
            if kind in hard:
                hard[kind] += 1
    return hard


def _tag_counts(items: Sequence[CaseRun]) -> dict[str, dict[str, int]]:
    """按标签累计用例数与通过数。"""
    tags: dict[str, dict[str, int]] = {}
    for run in items:
        for tag in run.case.tags:
            bucket = tags.setdefault(tag, {"n": 0, "passed": 0})
            bucket["n"] += 1
            bucket["passed"] += 1 if run.score.passed else 0
    return tags


# 门槛的缺省口径：manifest 未声明时用它，保证「没配阈值」不等于「没有门槛」。
DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "pass_rate": {"min": 0.8, "min_samples": 0},
    "judge_correctness": {"min": 4.0, "min_samples": 10},
    "trajectory": {"min": 0.85, "min_samples": 10},
}


def _normalized_thresholds(raw: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    """把 manifest 的阈值归一成 `{key: {min, min_samples}}`。

    manifest 声明了就只用声明的（想要单门禁就只写一个）；
    完全没声明时才回落到 DEFAULT_THRESHOLDS。
    """
    if not raw:
        return {key: dict(spec) for key, spec in DEFAULT_THRESHOLDS.items()}
    merged: dict[str, dict[str, float]] = {}
    for key, spec in raw.items():
        if hasattr(spec, "model_dump"):
            spec = spec.model_dump()
        if not isinstance(spec, dict):
            continue
        minimum = spec.get("min")
        if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
            continue
        samples = spec.get("min_samples", 0)
        merged[str(key)] = {
            "min": float(minimum),
            "min_samples": float(samples) if isinstance(samples, (int, float)) else 0.0,
        }
    return merged


def _gate_rows(
    *,
    passed: int,
    n: int,
    judge_means: dict[str, float] | None,
    judge_counts: dict[str, int],
    trajectory: float | None,
    trajectory_n: int,
    thresholds: dict[str, dict[str, float]],
    min_cases_for_gate: int,
) -> list[dict[str, Any]]:
    """逐门禁给出 value / min / samples / 三态。

    三态而不是布尔：`insufficient` 表示「样本不够，不能判」——
    把它压成 false 会把「没测够」错报成「质量差」。
    """
    rows: list[dict[str, Any]] = []
    for key, spec in thresholds.items():
        value, samples = _gate_value(
            key, passed, n, judge_means, judge_counts, trajectory, trajectory_n
        )
        if n < min_cases_for_gate:
            state = "insufficient"
        elif value is None or samples <= 0:
            state = "insufficient"
        elif samples < int(spec["min_samples"]):
            state = "insufficient"
        else:
            state = "pass" if value >= spec["min"] else "fail"
        rows.append(
            {
                "key": key,
                "value": round(value, 4) if value is not None else None,
                "min": spec["min"],
                "min_samples": int(spec["min_samples"]),
                "samples": samples,
                "state": state,
            }
        )
    return rows


def _gate_value(
    key: str,
    passed: int,
    n: int,
    judge_means: dict[str, float] | None,
    judge_counts: dict[str, int],
    trajectory: float | None,
    trajectory_n: int,
) -> tuple[float | None, int]:
    """门禁对应的实际值与样本数。"""
    if key == "pass_rate":
        return (round(passed / n, 4) if n else None), n
    if key == "judge_correctness":
        means = judge_means or {}
        return means.get("correctness"), judge_counts.get("correctness", 0)
    if key == "trajectory":
        return trajectory, trajectory_n
    return None, 0


def _verdict_from_gates(gates: Sequence[dict[str, Any]], has_hard: bool) -> str:
    """硬失败 > 全体样本不足 > 任一门禁未达 > 通过。"""
    if has_hard:
        return "fail"
    states = {str(gate.get("state")) for gate in gates}
    if gates and states <= {"insufficient"}:
        return "insufficient"
    if "fail" in states:
        return "fail"
    return "pass"


def _judge_counts(runs: Iterable[CaseRun]) -> dict[str, int]:
    """每个 judge 维度各有几条样本（决定该门禁能不能判）。"""
    counts: dict[str, int] = {dimension: 0 for dimension in JUDGE_DIMENSIONS}
    for run in runs:
        for dimension in run.judge_scores or {}:
            if dimension in counts:
                counts[dimension] += 1
    return counts


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def aggregate(
    runs: Iterable[CaseRun],
    min_cases_for_gate: int = 40,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一组用例运行聚合成 summary_json。

    `thresholds` 来自 manifest；缺省用 DEFAULT_THRESHOLDS。
    verdict 由 gates 归约而来，不再是一句 `n < 40 → insufficient` 的硬编码。
    """
    items = list(runs)
    n = len(items)
    passed = sum(1 for run in items if run.score.passed)

    trajectories = [
        run.score.trajectory_score for run in items if run.score.trajectory_score is not None
    ]
    trajectory = _round_or_none(_mean(trajectories))
    judge_means = _judge_means(items)
    gates = _gate_rows(
        passed=passed,
        n=n,
        judge_means=judge_means,
        judge_counts=_judge_counts(items),
        trajectory=trajectory,
        trajectory_n=len(trajectories),
        thresholds=_normalized_thresholds(thresholds),
        min_cases_for_gate=min_cases_for_gate,
    )
    has_hard = any(run.score.hard_failures for run in items)

    return {
        "n": n,
        "passed": passed,
        "pass_rate": round(passed / n, 4) if n else 0.0,
        "hard_failures": _count_hard(items),
        "judge": judge_means,
        "trajectory_score": trajectory,
        "p95_ms": _p95([run.latency_ms for run in items]),
        "tokens_total": sum(run.tokens for run in items),
        "tags": _tag_counts(items),
        "subjects": _subject_counts(items),
        "min_cases_for_gate": min_cases_for_gate,
        "gates": gates,
        "verdict": _verdict_from_gates(gates, has_hard),
        "cases": [
            {"case_id": run.case.id, "passed": run.score.passed, "status": run.trace.status}
            for run in items
        ],
    }


def _subject_counts(items: Sequence[CaseRun]) -> dict[str, dict[str, int]]:
    """按用例声明的 subject 归组：这条结论能归因到哪个因子。"""
    subjects: dict[str, dict[str, int]] = {}
    for run in items:
        bucket = subjects.setdefault(run.case.subject, {"n": 0, "passed": 0})
        bucket["n"] += 1
        bucket["passed"] += 1 if run.score.passed else 0
    return subjects


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


def _delta(base_value: float | None, head_value: float | None) -> float | None:
    """两边都有值才算 delta，缺一边就返回 None（渲染成「-」）。"""
    if base_value is None or head_value is None:
        return None
    return head_value - base_value


def _int_field(summary: dict[str, Any], key: str, default: int) -> int:
    """取整数字段；缺字段或类型不是数字时用默认值。"""
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _metric_rows(
    report: dict[str, Any], baseline: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, float | None]]:
    """逐指标算 base/head/delta 与更好方向。"""
    metrics: list[dict[str, Any]] = []
    deltas: dict[str, float | None] = {}
    for key, higher_is_better in _METRIC_KEYS:
        base_value = _metric_value(baseline, key)
        head_value = _metric_value(report, key)
        delta = _delta(base_value, head_value)
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
    return metrics, deltas


def _has_hard_failure(report: dict[str, Any]) -> bool:
    """当前报告是否有硬失败（任一分类 > 0 或整体结论 fail）。"""
    hard = report.get("hard_failures")
    if isinstance(hard, dict) and any(int(value or 0) > 0 for value in hard.values()):
        return True
    return report.get("verdict") == "fail"


# 可比性因子：变了哪一类，决定结论能说多硬。
_FACTOR_FIELDS: tuple[str, ...] = (
    "suite_sha",
    "grader_version",
    "prompt_sha",
    "model",
    "profile",
    "judge_model",
)
# 这些因子变了就不是同一个实验，delta 不能归因。
_BLOCKING_FACTORS: frozenset[str] = frozenset({"suite_sha", "grader_version", "judge_model"})


def _factor(summary: dict[str, Any], key: str) -> str:
    """取因子值；缺失或空串返回空串（表示「这批没记录」）。"""
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value)


def minimal_detectable_effect(base_n: int, head_n: int) -> float:
    """精度上限：n 条用例时，小于 1/n 的差异不超过一个用例的抖动。"""
    denom = min(max(base_n, 0), max(head_n, 0))
    return round(1 / denom, 4) if denom else 1.0


def compatibility(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """两份报告是否可比，以及可比到什么程度。

    - `blocking`：题库/判卷口径/裁判换了 —— 结论是 `not_comparable`；
    - `confounded`：提示词与模型（或档位）同时变了 —— 只报变化，不归因；
    - `clean`：只有提示词变，或什么都没变（复测，可用来估噪声）；
    - `unknown`：有一侧没记录因子，无法判断。
    """
    changed: list[str] = []
    missing: list[str] = []
    for key in _FACTOR_FIELDS:
        left = _factor(baseline, key)
        right = _factor(report, key)
        if not left or not right:
            missing.append(key)
            continue
        if left != right:
            changed.append(key)

    blocking = sorted(_BLOCKING_FACTORS.intersection(changed))
    if blocking:
        level = "blocking"
        reasons = [f"{', '.join(blocking)} 变了，两侧不是同一个实验，delta 不可归因"]
    elif not changed:
        level = "clean" if not missing else "unknown"
        reasons = (
            ["所有因子一致，可作为复测样本估计噪声"]
            if not missing
            else [f"缺少因子记录: {', '.join(missing)}，无法确认可比性"]
        )
    elif set(changed) <= {"prompt_sha"}:
        level = "clean"
        reasons = ["只有 prompt_sha 变化，delta 可归因到提示词"]
    else:
        level = "confounded"
        reasons = [f"同时变化: {', '.join(changed)}，只能报变化不能归因到单一因子"]

    return {"level": level, "changed": changed, "missing": missing, "reasons": reasons}


def _compare_verdict(
    report: dict[str, Any],
    deltas: dict[str, float | None],
    tolerance: float,
    compat: dict[str, Any],
) -> str:
    """对比结论：硬失败 > 不可比 > 样本不足 > 超精度回退 > 通过。"""
    if _has_hard_failure(report):
        return "fail"
    if compat.get("level") == "blocking":
        return "not_comparable"
    if _int_field(report, "n", 0) < _int_field(report, "min_cases_for_gate", 40):
        return "insufficient"
    pass_delta = deltas.get("pass_rate")
    correctness_delta = deltas.get("correctness")
    regressed = (pass_delta is not None and pass_delta < -tolerance) or (
        correctness_delta is not None and correctness_delta < -0.2
    )
    return "warn" if regressed else "pass"


def _case_rows(base_cases: dict[str, bool], head_cases: dict[str, bool]) -> list[dict[str, Any]]:
    """两侧用例并集，逐条给出 base/head 通过与否（缺一侧为 None）。"""
    return [
        {
            "case_id": case_id,
            "base_passed": base_cases.get(case_id),
            "head_passed": head_cases.get(case_id),
        }
        for case_id in sorted(set(base_cases) | set(head_cases))
    ]


def compare(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """当前报告 vs 基线：指标 delta、用例变化、可比性与结论。"""
    metrics, deltas = _metric_rows(report, baseline)
    cases = _case_rows(_case_map(baseline), _case_map(report))
    hard = report.get("hard_failures")
    n = _int_field(report, "n", 0)
    base_n = _int_field(baseline, "n", 0)
    mde = minimal_detectable_effect(base_n, n)
    compat = compatibility(report, baseline)

    return {
        "verdict": _compare_verdict(report, deltas, max(0.02, mde), compat),
        "metrics": metrics,
        "cases": cases,
        "n": n,
        "base_n": base_n,
        "mde": mde,
        "compatibility": compat,
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


def _render_compare(metrics: list[Any]) -> list[str]:
    """基线对比表。"""
    lines = [
        "## 基线对比",
        "",
        "| 指标 | 基线 | 当前 | Δ | 更好 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in metrics:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| {item.get('key')} | {_fmt(item.get('base'))} | {_fmt(item.get('head'))} "
            f"| {_fmt(item.get('delta'))} | {_fmt(item.get('better'))} |"
        )
    lines.append("")
    return lines


def _render_hard(hard: dict[str, Any]) -> list[str]:
    """硬失败分类表。"""
    lines = ["## 硬失败", "", "| 分类 | 次数 |", "| --- | --- |"]
    for key in _HARD_KINDS:
        lines.append(f"| {key} | {hard.get(key, 0)} |")
    lines.append("")
    return lines


def _render_judge(judge: dict[str, Any]) -> list[str]:
    """Judge 各维度均值表。"""
    lines = ["## Judge 均值", "", "| 维度 | 分数 |", "| --- | --- |"]
    for dimension in JUDGE_DIMENSIONS:
        if dimension in judge:
            lines.append(f"| {dimension} | {_fmt(judge[dimension])} |")
    lines.append("")
    return lines


def _render_tags(tags: dict[str, Any]) -> list[str]:
    """标签分布表。"""
    lines = ["## 标签分布", "", "| 标签 | 用例 | 通过 |", "| --- | --- | --- |"]
    for tag, bucket in sorted(tags.items()):
        if isinstance(bucket, dict):
            lines.append(f"| {tag} | {bucket.get('n', 0)} | {bucket.get('passed', 0)} |")
    lines.append("")
    return lines


def _render_gates(gates: list[Any]) -> list[str]:
    """门禁三态表：通过的、未达的、样本不足的分开看。"""
    state_text = {"pass": "通过", "fail": "未达", "insufficient": "样本不足"}
    lines = ["## 门禁", "", "| 指标 | 值 | 阈值 | 样本 | 结论 |", "| --- | --- | --- | --- | --- |"]
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        lines.append(
            f"| {gate.get('key')} | {_fmt(gate.get('value'))} | {_fmt(gate.get('min'))} "
            f"| {_fmt(gate.get('samples'))} | {state_text.get(str(gate.get('state')), '-')} |"
        )
    lines.append("")
    return lines


def _render_compatibility(compat: dict[str, Any]) -> list[str]:
    """可比性结论：不可比时必须显式写出来，否则 delta 会被当成结论。"""
    level = str(compat.get("level") or "")
    if level in ("", "clean"):
        return []
    label = {"blocking": "不可比", "confounded": "混杂", "unknown": "未知"}.get(level, level)
    reasons = compat.get("reasons")
    detail = "；".join(str(item) for item in reasons) if isinstance(reasons, list) else ""
    return [f"- 可比性：**{label}** —— {detail}", ""]


# 报告的节次顺序：容器类型不对就跳过（残缺 summary 也要能渲染），
# `keep_empty` 表示空容器仍渲染表头（对比表没有数据也要说明「没有变化」）。
_SECTIONS: tuple[tuple[str, type, bool, Callable[[Any], list[str]]], ...] = (
    ("compatibility", dict, True, _render_compatibility),
    ("gates", list, False, _render_gates),
    ("metrics", list, True, _render_compare),
    ("hard_failures", dict, True, _render_hard),
    ("judge", dict, False, _render_judge),
    ("tags", dict, False, _render_tags),
)


def _render_header(summary: dict[str, Any]) -> list[str]:
    lines = ["# 评测报告", "", f"- 结论：**{summary.get('verdict', '-')}**"]
    if "n" in summary:
        lines.append(
            f"- 用例：{summary.get('passed', 0)}/{summary.get('n', 0)} 通过"
            f"（pass_rate {_fmt(summary.get('pass_rate'))}）"
        )
    mde = summary.get("mde")
    if isinstance(mde, (int, float)):
        lines.append(
            f"- 精度上限：n={summary.get('n', '-')}，小于 {float(mde) * 100:.1f}pp 的变化不显著"
        )
    return [*lines, ""]


def _render_sections(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, kind, keep_empty, render in _SECTIONS:
        value = summary.get(key)
        if isinstance(value, kind) and (value or keep_empty):
            lines.extend(render(value))
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    """把聚合报告（或对比报告）渲染成可提交到 PR 的 Markdown。"""
    lines = _render_header(summary)
    lines.extend(_render_sections(summary))
    lines.append(
        f"- 轨迹分：{_fmt(summary.get('trajectory_score'))}；"
        f"p95：{_fmt(summary.get('p95_ms'))} ms；"
        f"tokens：{_fmt(summary.get('tokens_total'))}"
    )
    return "\n".join(lines).rstrip() + "\n"
