"""效果质量与安全合规维度 —— 评分样本加权与权限拒绝扣分。"""

from __future__ import annotations

from devops_agent.config import HealthPolicy
from devops_agent.eval.scorer import EvalDimension
from devops_agent.observability.health.model import (
    DIMENSION_LABELS,
    DimensionScore,
    ScoreItem,
    _clamp,
    _round,
)
from devops_agent.observability.models import EvalScoreRecord, SpanRecord


def quality_detail(
    scores: list[EvalScoreRecord], policy: HealthPolicy, weight: float = 0.25
) -> DimensionScore:
    """效果质量：correctness 评分按来源加权，并向中性先验收缩。"""
    samples = [
        (score.score, policy.source_weights.get(score.source, 0.5))
        for score in scores
        if score.dimension == EvalDimension.CORRECTNESS.value and 0.0 <= score.score <= 1.0
    ]
    if not samples:
        return DimensionScore(
            key="quality",
            label=DIMENSION_LABELS["quality"],
            value=None,
            weight=weight,
            confidence=0.0,
            note="没有 correctness 评分，该维度不参与加权",
            items=(ScoreItem("样本", 0.0, "0 条评分"),),
        )
    sample_weight = sum(w for _, w in samples)
    prior_weight = policy.quality_prior_weight
    raw = sum(v * w for v, w in samples) / sample_weight
    shrunk = (raw * sample_weight + policy.quality_prior * prior_weight) / (
        sample_weight + prior_weight
    )
    confidence = sample_weight / (sample_weight + prior_weight) if prior_weight > 0 else 1.0
    items = (
        ScoreItem("样本", 0.0, f"{len(samples)} 条评分 · 权重和 {sample_weight:.1f}"),
        ScoreItem("原始均值", 0.0, f"{raw:.2f}"),
        ScoreItem(
            "先验收缩",
            _round((shrunk - raw) * 100.0),
            f"{raw:.2f} → {shrunk:.2f}（先验 {policy.quality_prior:.2f}）",
        ),
    )
    return DimensionScore(
        key="quality",
        label=DIMENSION_LABELS["quality"],
        value=_round(_clamp(shrunk * 100.0)),
        weight=weight,
        confidence=_round(confidence, 3),
        note=f"{len(samples)} 条评分",
        items=items,
    )


def safety_detail(spans: list[SpanRecord], weight: float = 0.15) -> DimensionScore:
    """安全合规：拒绝占比扣分，重复越界再追加。"""
    permission = [span for span in spans if span.type == "permission"]
    if not permission:
        return DimensionScore(
            key="safety",
            label=DIMENSION_LABELS["safety"],
            value=100.0,
            weight=weight,
            # 没有权限判定记录不等于安全，只是没有反证：置信度给低
            confidence=0.4,
            note="没有权限判定记录，按无违规处理",
            items=(ScoreItem("权限判定", 0.0, "0 次判定"),),
        )
    denied = [span for span in permission if span.status == "error"]
    rate = len(denied) / len(permission)
    penalty = 60.0 * rate
    repeat = min(15.0, max(0, len(denied) - 1) * 5.0)
    value = _clamp(100.0 - penalty - repeat)
    items = (
        ScoreItem("拒绝占比", -_round(penalty), f"{len(denied)}/{len(permission)} 次被拒"),
        ScoreItem("重复越界", -_round(repeat), f"额外 {max(0, len(denied) - 1)} 次"),
    )
    return DimensionScore(
        key="safety",
        label=DIMENSION_LABELS["safety"],
        value=_round(value),
        weight=weight,
        confidence=1.0,
        note=f"{len(permission)} 次权限判定",
        items=items,
    )
