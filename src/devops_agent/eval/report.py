"""评测报告 —— 聚合、基线对比与 Markdown 渲染。

报告是纯函数：输入 CaseRun 列表或 summary dict，输出 JSON 友好结构。
所有字段名都与 `/api/eval` 的响应契约对齐，CLI 与 API 共用同一套口径。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import ceil
from typing import Any

from devops_agent.eval.judge import JUDGE_DIMENSIONS
from devops_agent.eval.kinds import (
    BLOCKING_KEYS,
    zero_counts,
)
from devops_agent.eval.report_render import render_markdown
from devops_agent.eval.report_risk import (
    ATTRIBUTED,
    FORMAT_GATE_MIN,
    _attribution_counts,
    _capability_metrics,
    _count_by,
    _format_gate,
    _funnel,
    _rate,
    _risk_counts,
    _risk_gate_rows,
)
from devops_agent.eval.runner import CaseRun

# 这些名字历史上从 `report` 单一模块导出；拆文件后保持路径不变，
# 调用方（CLI / API / 测试）无需改 import。
__all__ = [
    "ATTRIBUTED",
    "FORMAT_GATE_MIN",
    "aggregate",
    "compare",
    "compatibility",
    "minimal_detectable_effect",
    "render_markdown",
]

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
    """逐分类统计硬失败用例数（同类只算一次）。

    遍历全部 7 类而不是只统计 3 类：判卷层会产出 `status`/`text`/`tool`/`grounding`，
    漏掉它们会让「14/15 通过、结论 fail」的报告显示成「三行全 0 硬失败」。
    """
    hard = zero_counts()
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
        # reason 区分三种「不能判」：报表不能只写一句「样本不足」，
        # 否则「题库太小」「裁判没跑」「裁判样本不够」三种完全不同的处置
        # 在报告里长得一模一样。
        reason: str | None = None
        if n < min_cases_for_gate:
            state, reason = "insufficient", "n_below_gate"
        elif value is None or samples <= 0:
            state, reason = "insufficient", "no_samples"
        elif samples < int(spec["min_samples"]):
            state, reason = "insufficient", "samples_below_min"
        else:
            state = "pass" if value >= spec["min"] else "fail"
        # 阈值本身的结论，与「样本够不够判」无关。
        # 小样本下 pass_rate=0.0 依然是明确的坏结果：不能因为 n < 门禁量就
        # 只剩一句「样本不足」，那会让一次全灭的 nightly 退 0 码（绿灯）。
        threshold_state = None if value is None else ("pass" if value >= spec["min"] else "fail")
        rows.append(
            {
                "key": key,
                "value": round(value, 4) if value is not None else None,
                "min": spec["min"],
                "min_samples": int(spec["min_samples"]),
                "samples": samples,
                "state": state,
                "reason": reason,
                "threshold_state": threshold_state,
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


def _verdict_from_gates(gates: Sequence[dict[str, Any]], blocking_hard: bool) -> str:
    """阻断性硬失败 > 小样本下的阈值未达 > 全体样本不足 > 任一门禁未达 > 通过。

    只有 blocking 类（safety / forbidden_tool / status）才一票否决。
    scoring 类（tool / text / grounding / format）已经体现在该用例的
    `passed=False` 上，进而体现在 `pass_rate` 里——由阈值统一判，不再单独否决。
    旧实现用「任意 hard 失败即 fail」，让 `thresholds.pass_rate` 永不生效：
    实测 pass_rate=0.9333（> 阈值 0.8）仍被判 fail。

    小样本（`n < min_cases_for_gate`）也要看 `threshold_state`：题库没到门禁量时
    `pass_rate=0.0` 是明确的坏结果，不能因为「样本不足」就退 0 码。
    但这条只对 `pass_rate` 生效——`judge_correctness` / `trajectory` 在小样本下
    本来就不可判（3 条裁判均值没有意义），不能拿它否决整批。
    """
    if blocking_hard:
        return "fail"
    small_sample_fail = any(
        str(gate.get("state")) == "insufficient"
        and str(gate.get("key")) == "pass_rate"
        and str(gate.get("threshold_state")) == "fail"
        for gate in gates
    )
    if small_sample_fail:
        return "fail"
    states = {str(gate.get("state")) for gate in gates}
    if gates and states <= {"insufficient"}:
        return "insufficient"
    if "fail" in states:
        return "fail"
    return "pass"


def _verdict_from_risk(
    gates: Sequence[dict[str, Any]], risk_gates: Sequence[dict[str, Any]], blocking_hard: bool
) -> str:
    """红线 > 任一分层未达 > 任一分层不可判 > 通过。

    分层模式下不看 `blocking_hard`：`status`（等待确认/超时）属于 harness，
    已在归因里排除；真正的红线由各层 `safety_failures` 逐层拦截。
    """
    if not risk_gates:
        return _verdict_from_gates(gates, blocking_hard)
    states = [str(gate.get("state")) for gate in risk_gates]
    if "fail" in states:
        return "fail"
    if any(state == "insufficient" for state in states):
        return "insufficient"
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


def _is_blocking(run: CaseRun) -> bool:
    """这条用例是否带阻断性硬失败。"""
    return bool(BLOCKING_KEYS.intersection(run.score.hard_failures))


def _is_scoring_only(run: CaseRun) -> bool:
    """只有计分类硬失败（不含任何阻断类）。"""
    return bool(set(run.score.hard_failures) - BLOCKING_KEYS)


def _mean_trajectory(items: Sequence[CaseRun]) -> tuple[float | None, int]:
    """轨迹分均值与样本数。分开返回是因为门禁要同时判「值」与「够不够」。"""
    values = [run.score.trajectory_score for run in items if run.score.trajectory_score is not None]
    return _round_or_none(_mean(values)), len(values)


def _case_rows_of(items: Sequence[CaseRun]) -> list[dict[str, Any]]:
    """逐用例快照：compare 用它算翻转，前端用它做漏斗下钻。"""
    return [
        {
            "case_id": run.case.id,
            "risk": run.case.risk,
            "passed": run.score.passed,
            "status": run.trace.status,
            "safety_ok": run.score.safety_ok,
            "answer_ok": run.score.answer_ok,
            "trajectory_ok": run.score.trajectory_ok,
            "format_ok": run.score.format_ok,
            "attribution": run.score.attribution,
        }
        for run in items
    ]


def _cache_stats(items: Sequence[CaseRun]) -> dict[str, float | int | None]:
    """前缀缓存的命中情况。

    命中率 = 命中量 / (命中量 + 未命中量)。未命中量用「总输入 − 命中」近似：
    事件里没有单独的 miss 字段，但 `cache_input_tokens` 是 input 的子集。

    这些数字此前完全不存在，于是「把档位说明从系统提示挪到消息末尾」
    （前缀命中 21% → 96%）这个改动在报告里没有任何位置可以观察。
    """
    cached = sum(run.trace.cache_input_tokens for run in items)
    created = sum(run.trace.cache_creation_tokens for run in items)
    total = sum(run.tokens for run in items)
    if total <= 0:
        return {"cache_input": cached, "cache_creation": created, "cache_hit_rate": None}
    return {
        "cache_input": cached,
        "cache_creation": created,
        "cache_hit_rate": round(cached / total, 4),
    }


def _summary_payload(
    items: Sequence[CaseRun],
    *,
    gates: list[dict[str, Any]],
    risk_gates: list[dict[str, Any]],
    judge_means: dict[str, float] | None,
    trajectory: float | None,
    min_cases_for_gate: int,
) -> dict[str, Any]:
    """拼装配好的 summary 字段。

    单独一个函数是为了把「一堆带条件的分支」和「编排」隔开：
    圈复杂度门禁是每函数 ≤10，混在一起会双双超标。
    """
    n = len(items)
    passed = _count_by(items, lambda run: run.score.passed)
    has_blocking = any(_is_blocking(run) for run in items)
    return {
        "n": n,
        "passed": passed,
        "pass_rate": _rate(passed, n),
        # v3 门禁主指标：排除 harness/assertion/format/budget 后的真实能力通过率。
        **_capability_metrics(items),
        "hard_failures": _count_hard(items),
        "hard_blocking": _count_by(items, _is_blocking),
        "hard_scoring": _count_by(items, _is_scoring_only),
        "judge": judge_means,
        "trajectory_score": trajectory,
        "p95_ms": _p95([run.latency_ms for run in items]),
        "tokens_total": sum(run.tokens for run in items),
        # 每条「能力通过」摊到的 token；true 成本比平均分更能防退步。
        **_cache_stats(items),
        "tags": _tag_counts(items),
        "subjects": _subject_counts(items),
        "risk": _risk_counts(items),
        "funnel": _funnel(items),
        "attribution": _attribution_counts(items),
        "min_cases_for_gate": min_cases_for_gate,
        "gates": [*gates, *risk_gates],
        "verdict": _verdict_from_risk(gates, risk_gates, has_blocking),
        "cases": _case_rows_of(items),
    }


def aggregate(
    runs: Iterable[CaseRun],
    min_cases_for_gate: int = 40,
    thresholds: dict[str, Any] | None = None,
    risk_thresholds: dict[str, float] | None = None,
    min_cases_per_risk: dict[str, int] | None = None,
) -> dict[str, Any]:
    """把一组用例运行聚合成 summary_json。

    `thresholds` 来自 manifest；缺省用 DEFAULT_THRESHOLDS。
    声明了 `risk_thresholds` 时改为分层门禁，旧的全局 `pass_rate` 只展示不判。
    """
    items = list(runs)
    passed = sum(1 for run in items if run.score.passed)
    trajectory, trajectory_n = _mean_trajectory(items)
    judge_means = _judge_means(items)
    risk_gates: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    if risk_thresholds:
        risk_gates = _risk_gate_rows(
            _risk_counts(items),
            risk_thresholds,
            min_cases_per_risk or {},
            min_cases_for_gate,
            len(items),
        )
        # 引用/格式单列门禁：能力口径排除了 format，必须另设一根线，
        # 否则「答案对了但从不给 路径:行号」会被能力通过率完全掩盖。
        risk_gates.append(_format_gate(items))
    else:
        gates = _gate_rows(
            passed=passed,
            n=len(items),
            judge_means=judge_means,
            judge_counts=_judge_counts(items),
            trajectory=trajectory,
            trajectory_n=trajectory_n,
            thresholds=_normalized_thresholds(thresholds),
            min_cases_for_gate=min_cases_for_gate,
        )
    return _summary_payload(
        items,
        gates=gates,
        risk_gates=risk_gates,
        judge_means=judge_means,
        trajectory=trajectory,
        min_cases_for_gate=min_cases_for_gate,
    )


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
    """当前报告是否有**阻断性**硬失败（决定 compare 直接判 fail）。

    只看 blocking 类：scoring 类失败已经体现在 pass_rate 上，该由 delta 判，
    否则「一条用例事实缺失」会让整批对比无条件 fail，阈值和 delta 都失去意义。
    """
    hard = report.get("hard_failures")
    if isinstance(hard, dict):
        if any(int(hard.get(key) or 0) > 0 for key in BLOCKING_KEYS):
            return True
    return report.get("verdict") == "fail"


# 可比性因子：变了哪一类，决定结论能说多硬。
_FACTOR_FIELDS: tuple[str, ...] = (
    "suite_sha",
    "grader_version",
    "prompt_sha",
    # 模型参数指纹（temperature / max_tokens / 窗口 / 迭代上限…）。
    # 参数可配之后，「同一提示词分数变了」既可能是提示词也可能是参数，必须能分开。
    "params_sha",
    "model",
    "profile",
    "judge_model",
    # 裁判覆盖范围变了，judge 均值就换了分母，两批的 judge 不可直接比
    "judge_scope",
    # 信息性因子：同一语义提示词在不同平台上下发字节不同，前缀缓存不能跨机
    # 复用，但这不代表提示词改过 —— 只报告，不阻断对比。
    "prompt_bytes_sha",
    "shell_platform",
)
# 这些因子变了就不是同一个实验，delta 不能归因。
_BLOCKING_FACTORS: frozenset[str] = frozenset(
    {"suite_sha", "grader_version", "judge_model", "judge_scope"}
)
# 信息性因子：只报告差异，不阻断对比，也不作为「提示词变更」的证据。
_INFORMATIONAL_FACTORS: frozenset[str] = frozenset({"prompt_bytes_sha", "shell_platform"})


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


def _factor_diff(report: dict[str, Any], baseline: dict[str, Any]) -> tuple[list[str], list[str]]:
    """逐因子比对：返回 (变化的因子, 两侧有一侧没记录的因子)。"""
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
    return changed, missing


def _classify(changed: list[str], missing: list[str]) -> tuple[str, list[str]]:
    """按变化的因子组合判定可比性等级与理由。

    拆出来是为了把「一堆 elif」从 compatibility 里挪走（圈复杂度门禁 ≤10）。
    """
    blocking = sorted(_BLOCKING_FACTORS.intersection(changed))
    if blocking:
        return "blocking", [f"{', '.join(blocking)} 变了，两侧不是同一个实验，delta 不可归因"]
    if not changed:
        if missing:
            return "unknown", [f"缺少因子记录: {', '.join(missing)}，无法确认可比性"]
        return "clean", ["所有因子一致，可作为复测样本估计噪声"]
    if set(changed) <= {"prompt_sha"}:
        return "clean", ["只有 prompt_sha 变化，delta 可归因到提示词"]
    if set(changed) <= {"params_sha"}:
        # 参数变了仍可比（同一题库、同一提示词），但结论只能落到参数上
        return "clean", ["只有 params_sha 变化，delta 可归因到模型参数"]
    if set(changed) <= _INFORMATIONAL_FACTORS:
        # 只有平台/字节差异：提示词没改，但前缀缓存不可复用。
        # 这不该拦住对比（不是提示词变更），也不该说成「可归因到提示词」。
        return "clean", [
            f"只有 {', '.join(sorted(changed))} 变化：同一语义提示词、不同机器/平台，"
            "delta 可归因到环境差异而非提示词；前缀缓存不能跨机复用"
        ]
    return "confounded", [f"同时变化: {', '.join(changed)}，只能报变化不能归因到单一因子"]


def compatibility(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """两份报告是否可比，以及可比到什么程度。

    - `blocking`：题库/判卷口径/裁判换了 —— 结论是 `not_comparable`；
    - `confounded`：提示词与模型（或档位）同时变了 —— 只报变化，不归因；
    - `clean`：只有提示词变（或只有平台/字节差异，或什么都没变）；
    - `unknown`：有一侧没记录因子，无法判断。
    """
    changed, missing = _factor_diff(report, baseline)
    level, reasons = _classify(changed, missing)
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
