"""记忆门禁与结论判定（SPEC §9）。

优先级与 openjudge 一致：红线硬失败 > 样本不足 > 指标未达 > 证据不足 > 通过。
红线先于增益：跨会话泄漏、隐私入库存、约束丢失任何一项非零，整批直接 fail。
"""

from __future__ import annotations

from typing import Any

from devops_agent.openjudge.memory.models import MemoryPlan

#: 各赛道在 plan.thresholds 里对应的指标键；constraint_retention 是硬红线，单独判。
TRACK_METRICS: dict[str, tuple[str, ...]] = {
    "MT": ("probe_acc", "contradiction_rate"),
    "MS": ("probe_acc", "recall_at_5", "precision_at_5"),
    "CP": ("fact_retention", "artifact_retention"),
    "RT": ("recall_at_5", "precision_at_5", "mrr"),
    "NB": ("junk_rate",),
    "PR": (),
}
GAIN_TRACKS = ("MT", "MS")
#: 这些指标为 None 说明「没有样本」，降级为 insufficient 而不是当作达标。
REQUIRED_METRICS = frozenset(
    {"probe_acc", "recall_at_5", "precision_at_5", "mrr", "fact_retention"}
)
LOWER_IS_BETTER = frozenset({"contradiction_rate", "junk_rate"})


def _reason(
    code: str, actual: float | int | None, threshold: float | int | None, message: str
) -> dict[str, Any]:
    return {"code": code, "actual": actual, "threshold": threshold, "message": message}


def _metric_reasons(
    track: str, row: dict[str, Any], thresholds: dict[str, float]
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    for metric in TRACK_METRICS.get(track, ()):
        threshold = thresholds.get(metric)
        value = row.get(metric)
        if threshold is None:
            continue
        if value is None:
            if metric in REQUIRED_METRICS:
                reasons.append(_reason("metric_missing", None, threshold, f"{metric} 没有样本"))
            continue
        if metric in LOWER_IS_BETTER:
            if float(value) > threshold:
                reasons.append(
                    _reason(
                        metric, value, threshold, f"{metric} {value:.3f} 超过上限 {threshold:.3f}"
                    )
                )
        elif float(value) < threshold:
            reasons.append(
                _reason(metric, value, threshold, f"{metric} {value:.3f} 低于门槛 {threshold:.3f}")
            )
    return reasons


def _constraint_reason(row: dict[str, Any]) -> dict[str, Any] | None:
    value = row.get("constraint_retention")
    if value is None:
        return None
    if float(value) < 1.0:
        return _reason("constraint_lost", value, 1.0, f"压缩后约束保留率 {value:.2%}，必须 100%")
    return None


def _gain_reason(gain: dict[str, Any], plan: MemoryPlan) -> tuple[dict[str, Any] | None, bool]:
    """返回 (原因, 是否证据不足)。增益不足是 fail，统计不足是 unvalidated。"""
    threshold = plan.thresholds.get("memory_gain_pp")
    if threshold is None or not gain.get("n"):
        return (_reason("gain_missing", None, threshold, "没有配对的增益样本"), True)
    gain_pp = gain.get("gain_pp")
    if gain_pp is None or float(gain_pp) < threshold:
        return (
            _reason(
                "gain_below", gain_pp, threshold, f"记忆增益 {gain_pp}pp 低于门槛 {threshold}pp"
            ),
            False,
        )
    if plan.require_ci:
        p_value = gain.get("p")
        ci = gain.get("ci")
        if p_value is None or (ci is not None and ci[0] <= 0):
            return (
                _reason("gain_unvalidated", gain_pp, threshold, "增益未通过置信区间检验"),
                True,
            )
        if float(p_value) >= plan.alpha:
            return (
                _reason(
                    "gain_unvalidated", p_value, plan.alpha, f"McNemar p={p_value:.3f} 未达显著"
                ),
                True,
            )
    return (None, False)


def _red_line_causes(row: dict[str, Any]) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    if int(row.get("leak_count", 0)) > 0:
        causes.append(_reason("leak_above", row["leak_count"], 0, "跨会话/跨 scope 泄漏非零"))
    if int(row.get("redaction_violation", 0)) > 0:
        causes.append(
            _reason("redaction_above", row["redaction_violation"], 0, "未脱敏密钥出现在落盘物")
        )
    constraint = _constraint_reason(row)
    if constraint is not None:
        causes.append(constraint)
    return causes


def _nb_reasons(
    row: dict[str, Any], harm: dict[str, Any], plan: MemoryPlan
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if int(row.get("junk_injected", 0)) > 0:
        reasons.append(_reason("junk_injected", row["junk_injected"], 0, "不该注入的内容被注入"))
    limit = plan.thresholds.get("harm_rate", 0.01)
    if harm.get("harm_rate") is not None and float(harm["harm_rate"]) > limit:
        reasons.append(_reason("harm_above", harm["harm_rate"], limit, "无记忆任务被记忆拖累"))
    return reasons


def _track_reasons(
    track: str,
    row: dict[str, Any],
    plan: MemoryPlan,
    gains: dict[str, dict[str, Any]],
    harm: dict[str, Any],
) -> list[dict[str, Any]]:
    reasons = _metric_reasons(track, row, plan.thresholds)
    if track == "NB":
        reasons.extend(_nb_reasons(row, harm, plan))
    if track in GAIN_TRACKS:
        gain_reason, _unvalidated = _gain_reason(gains.get(track, {}), plan)
        if gain_reason is not None:
            reasons.append(gain_reason)
    return reasons


def _track_verdict(
    track: str,
    row: dict[str, Any],
    plan: MemoryPlan,
    gains: dict[str, dict[str, Any]],
    harm: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    if int(row.get("total", 0)) < plan.min_n:
        return "insufficient", [
            _reason(
                "sample_short", row.get("total"), plan.min_n, f"{track} 样本不足 {plan.min_n} 条"
            )
        ]
    reasons = _track_reasons(track, row, plan, gains, harm)
    return ("fail" if reasons else "pass"), reasons


def evaluate(
    plan: MemoryPlan,
    tracks: dict[str, dict[str, Any]],
    gains: dict[str, dict[str, Any]],
    harm: dict[str, Any],
    regression_failed: int,
) -> dict[str, Any]:
    """整批判定；返回 verdict 与逐条原因，前端直接展示原因不必再算阈值。"""
    causes: list[dict[str, Any]] = []
    verdicts: dict[str, str] = {}
    red_hit = False
    for track, row in tracks.items():
        red_causes = _red_line_causes(row)
        if red_causes:
            red_hit = True
            causes.extend(red_causes)
        verdict, reasons = _track_verdict(track, row, plan, gains, harm)
        verdicts[track] = verdict
        causes.extend({**item, "message": f"{track} 赛道：{item['message']}"} for item in reasons)
    if regression_failed:
        causes.append(
            _reason("regression_failed", regression_failed, 0, "回归集出现退化（不阻断但需展示）")
        )
    verdict = _overall(verdicts, red_hit, regression_failed)
    return {"verdict": verdict, "causes": causes, "tracks": verdicts}


def _overall(verdicts: dict[str, str], red_hit: bool, regression_failed: int) -> str:
    values = [value for key, value in verdicts.items() if not key.startswith("_")]
    if red_hit or "fail" in values:
        return "fail"
    if "insufficient" in values:
        return "insufficient"
    if "unvalidated" in values or regression_failed:
        return "unvalidated"
    return "pass"
