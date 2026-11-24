"""执行健康与效率成本维度 —— 状态、工具失败、未收敛、耗时与 token。"""

from __future__ import annotations

from devops_agent.config import HealthPolicy
from devops_agent.observability.health.model import (
    DIMENSION_LABELS,
    DimensionScore,
    ScoreItem,
    _clamp,
    _round,
)
from devops_agent.observability.models import RunRecord, SpanRecord

# 未收敛的判据：框架到达 max_iterations 时会把提示写进 run.error
_UNCONVERGED_MARKERS = ("max iteration", "max_iterations", "iteration numbers")

# 单个工具失败最多扣多少分：避免一次批量失败把执行健康打到 0 以下
_TOOL_FAIL_CAP = 24.0
# 工具失败率高于该阈值后再按比例追加扣分（偶发单点失败不该重罚）
_TOOL_RATE_FREE = 0.2
_TOOL_RATE_CAP = 18.0
# 未收敛额外扣分：跑满轮次说明任务没做完，即使没报错也要计
_UNCONVERGED_PENALTY = 15.0


def _model_spans(spans: list[SpanRecord]) -> list[SpanRecord]:
    return [span for span in spans if span.type == "model"]


def _tool_spans(spans: list[SpanRecord]) -> list[SpanRecord]:
    return [span for span in spans if span.type == "tool"]


def _is_unconverged(run: RunRecord) -> bool:
    """框架跑满最大轮次而被截断：报错里带 max iteration 字样。"""
    text = (run.error or "").lower()
    return any(marker in text for marker in _UNCONVERGED_MARKERS)


def _tool_penalty(spans: list[SpanRecord]) -> tuple[float, list[ScoreItem]]:
    """工具失败扣分：绝对次数 + 失败率各扣一段，返回 (总扣分, 明细)。"""
    tools = _tool_spans(spans)
    failed = [span for span in tools if span.status == "error"]
    items: list[ScoreItem] = []
    penalty = 0.0
    if failed:
        amount = min(_TOOL_FAIL_CAP, 8.0 * len(failed))
        penalty += amount
        items.append(ScoreItem("工具失败", -amount, f"{len(failed)}/{len(tools)} 次失败"))
    rate = len(failed) / len(tools) if tools else 0.0
    if rate > _TOOL_RATE_FREE:
        extra = min(_TOOL_RATE_CAP, (rate - _TOOL_RATE_FREE) * 30.0)
        penalty += extra
        items.append(ScoreItem("失败率偏高", -_round(extra), f"{rate:.0%} > {_TOOL_RATE_FREE:.0%}"))
    return penalty, items


def execution_detail(
    run: RunRecord, spans: list[SpanRecord], weight: float = 0.35
) -> DimensionScore:
    """执行健康：状态定基准，再按工具失败与未收敛扣分。"""
    if run.status == "running":
        return DimensionScore(
            key="execution",
            label=DIMENSION_LABELS["execution"],
            value=None,
            weight=weight,
            confidence=0.0,
            note="运行中，暂不评分",
        )

    succeeded = run.status == "success"
    base = 100.0 if succeeded else 0.0
    items: list[ScoreItem] = [ScoreItem("运行状态", 0.0, f"{run.status} → 基准 {base:.0f}")]
    value = base
    if succeeded:
        penalty, penalties = _tool_penalty(spans)
        value -= penalty
        items.extend(penalties)
        if _is_unconverged(run):
            value -= _UNCONVERGED_PENALTY
            items.append(ScoreItem("未收敛", -_UNCONVERGED_PENALTY, "跑满最大轮次被截断"))

    confidence = 1.0 if spans else 0.5
    note = "成功" if succeeded else "运行失败（基准 0，不重复扣分）"
    return DimensionScore(
        key="execution",
        label=DIMENSION_LABELS["execution"],
        value=_round(_clamp(value)),
        weight=weight,
        confidence=confidence,
        note=note,
        items=tuple(items),
    )


def latency_budget(model_calls: int, policy: HealthPolicy) -> int:
    """耗时预算：首步基准 + 之后每次模型调用叠加。

    单步任务用 latency_baseline_ms；5 步任务按 5 段给预算，避免长会话天生 0 分。
    """
    return policy.latency_baseline_ms + policy.per_step_baseline_ms * max(0, model_calls - 1)


def _latency_component(
    run: RunRecord, model_calls: int, policy: HealthPolicy
) -> tuple[float | None, float, str, float]:
    """耗时项：返回 (得分, 预算 ms, 说明, 置信度)。"""
    # 优先用 work_ms（span 并集，真正在干活），退回 duration_ms（含用户读消息的空闲）
    actual = run.work_ms or run.duration_ms or 0
    if actual <= 0:
        return None, 0.0, "没有耗时记录", 0.0
    budget = float(latency_budget(model_calls, policy))
    if actual <= budget:
        return 100.0, budget, f"{actual}ms ≤ 预算 {budget:.0f}ms", 1.0
    score = 100.0 * ((budget / actual) ** policy.latency_exponent)
    return _round(score), budget, f"{actual}ms / 预算 {budget:.0f}ms = {actual / budget:.1f}×", 1.0


def _token_component(
    run: RunRecord, model_calls: int, policy: HealthPolicy
) -> tuple[float | None, float, str]:
    """token 项：按「单次模型调用的均值」看，长会话不因总量大被误伤。"""
    calls = max(1, model_calls)
    total = run.input_tokens + run.output_tokens
    if total <= 0:
        return None, 0.0, "没有 token 记录"
    per_call = total / calls
    baseline = float(policy.token_baseline_per_call)
    if per_call <= baseline:
        return 100.0, per_call, f"每次 {per_call:.0f} ≤ 预算 {baseline:.0f}"
    score = 100.0 * ((baseline / per_call) ** policy.token_exponent)
    return _round(score), per_call, f"每次 {per_call:.0f} / 预算 {baseline:.0f}"


def _efficiency_parts(
    run: RunRecord, model_calls: int, policy: HealthPolicy
) -> tuple[list[tuple[float, float, float]], list[ScoreItem], float]:
    """算出耗时与 token 两项：(权重, 得分, 置信度) 列表、明细、时间预算。"""
    latency, budget, latency_note, latency_conf = _latency_component(run, model_calls, policy)
    tokens, _, token_note = _token_component(run, model_calls, policy)
    split = policy.efficiency_split
    parts: list[tuple[float, float, float]] = []
    if latency is not None:
        parts.append((split.get("latency", 0.7), latency, latency_conf))
    if tokens is not None:
        parts.append((split.get("tokens", 0.3), tokens, 1.0))
    items = [
        ScoreItem("耗时", _round(latency - 100.0) if latency is not None else 0.0, latency_note),
        ScoreItem("token 强度", _round(tokens - 100.0) if tokens is not None else 0.0, token_note),
    ]
    return parts, items, budget


def efficiency_detail(
    run: RunRecord, spans: list[SpanRecord], policy: HealthPolicy, weight: float = 0.25
) -> DimensionScore:
    """效率：耗时与 token 两项按占比合成，缺项时按剩余权重重归一化。"""
    model_calls = len(_model_spans(spans))
    parts, items, budget = _efficiency_parts(run, model_calls, policy)
    total_weight = sum(part_weight for part_weight, _, _ in parts)
    if total_weight <= 0:
        return DimensionScore(
            key="efficiency",
            label=DIMENSION_LABELS["efficiency"],
            value=None,
            weight=weight,
            confidence=0.0,
            note="没有耗时与 token 记录，无法评估效率",
            items=tuple(items),
        )
    value = sum(w * v for w, v, _ in parts) / total_weight
    # 置信度按「本次实际拿到的证据占配置总权的比例」算：
    # 只有 token 数据时效率分仍可给，但只值 30% 的把握。
    split = policy.efficiency_split
    split_total = sum(
        float(split.get(key, default)) for key, default in (("latency", 0.7), ("tokens", 0.3))
    )
    confidence = sum(w * c for w, _, c in parts) / split_total if split_total > 0 else 0.0
    note = f"{model_calls} 次模型调用 · 预算 {budget:.0f}ms" if budget else "没有调用记录"
    return DimensionScore(
        key="efficiency",
        label=DIMENSION_LABELS["efficiency"],
        value=_round(_clamp(value)),
        weight=weight,
        confidence=_round(confidence, 3),
        note=note,
        items=tuple(items),
    )
