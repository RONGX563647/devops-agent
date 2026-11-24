"""评分报告与兼容层 —— 把四个维度合成单一分数，并保留老的「只要一个数」入口。"""

from __future__ import annotations

from devops_agent.config import HealthPolicy
from devops_agent.observability.health.components import (
    _latency_component,
    efficiency_detail,
    execution_detail,
)
from devops_agent.observability.health.model import (
    DIMENSIONS,
    SCORING_VERSION,
    DimensionScore,
    HealthReport,
    _clamp,
    _round,
)
from devops_agent.observability.health.quality import quality_detail, safety_detail
from devops_agent.observability.models import EvalScoreRecord, RunRecord, SpanRecord


def grade_for(score: float, policy: HealthPolicy) -> str:
    """按配置阈值给等级；低于最低档返回 F。"""
    for grade, floor in sorted(policy.grades.items(), key=lambda item: -item[1]):
        if score >= floor:
            return grade
    return "F"


def _score_dimensions(
    run: RunRecord,
    spans: list[SpanRecord],
    scores: list[EvalScoreRecord],
    policy: HealthPolicy,
) -> dict[str, DimensionScore]:
    """算出四个维度；每种信号交给各自的纯函数，聚合层不再关心细节。"""
    weights = policy.weights.model_dump()
    return {
        "execution": execution_detail(run, spans, weights["execution"]),
        "efficiency": efficiency_detail(run, spans, policy, weights["efficiency"]),
        "quality": quality_detail(scores, policy, weights["quality"]),
        "safety": safety_detail(spans, weights["safety"]),
    }


def _usable(dimensions: dict[str, DimensionScore]) -> list[DimensionScore]:
    """可参与聚合的维度：有分数且配置权重为正。缺证据的维度不假装有分。"""
    return [dim for dim in dimensions.values() if dim.value is not None and dim.weight > 0]


def _configured_weight(policy: HealthPolicy) -> float:
    """配置里的全部维度权重和：算覆盖度时的分母。"""
    weights = policy.weights.model_dump()
    return sum(float(weights[key]) for key in DIMENSIONS if weights[key] > 0)


def _value_of(dim: DimensionScore) -> float:
    """取维度分值；调用方已用 `_usable` 过滤，None 只可能是 0 分的替代。"""
    return float(dim.value or 0.0)


def _combine(
    dimensions: dict[str, DimensionScore], policy: HealthPolicy
) -> tuple[float, float, float]:
    """把可用维度合成 (score, coverage, confidence)：缺维度剔除后重归一化。"""
    usable = _usable(dimensions)
    total_weight = sum(dim.weight for dim in usable)
    if not usable or total_weight <= 0:
        return 0.0, 0.0, 0.0
    all_weight = _configured_weight(policy)
    score = sum(dim.weight * _value_of(dim) for dim in usable) / total_weight
    confidence = sum(dim.weight * dim.confidence for dim in usable) / total_weight
    coverage = total_weight / all_weight if all_weight else 0.0
    return _round(_clamp(score)), _round(coverage, 3), _round(confidence, 3)


def report_run(
    run: RunRecord,
    spans: list[SpanRecord],
    scores: list[EvalScoreRecord],
    policy: HealthPolicy,
) -> HealthReport:
    """聚合四类子分为完整报告；缺失维度剔除后重归一化权重。"""
    dimensions = _score_dimensions(run, spans, scores, policy)
    score, coverage, confidence = _combine(dimensions, policy)
    return HealthReport(
        score=score,
        grade=grade_for(score, policy),
        coverage=coverage,
        confidence=confidence,
        version=SCORING_VERSION,
        dimensions=dimensions,
    )


# ---- 兼容层：老调用方只想要一个数字 -------------------------------------


def execution_score(run: RunRecord, spans: list[SpanRecord]) -> float:
    """执行健康分（v2 口径，忽略明细）。"""
    return execution_detail(run, spans).value or 0.0


def efficiency_score(run: RunRecord, baseline_ms: int) -> float:
    """效率分（仅耗时项、按固定基准；需要完整口径请用 report_run）。"""
    policy = HealthPolicy(latency_baseline_ms=baseline_ms, per_step_baseline_ms=0)
    value, _, _, _ = _latency_component(run, 1, policy)
    return 0.0 if value is None else value


def quality_score(scores: list[EvalScoreRecord]) -> float | None:
    """效果质量分（默认策略，无评分返回 None）。"""
    return quality_detail(scores, HealthPolicy()).value


def safety_score(spans: list[SpanRecord]) -> float:
    """安全合规分。"""
    return safety_detail(spans).value or 0.0


def score_run(
    run: RunRecord,
    spans: list[SpanRecord],
    scores: list[EvalScoreRecord],
    policy: HealthPolicy,
) -> float:
    """只要总分时的便捷入口。"""
    return report_run(run, spans, scores, policy).score
