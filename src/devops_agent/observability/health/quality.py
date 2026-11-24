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


def _correctness_samples(
    scores: list[EvalScoreRecord], policy: HealthPolicy
) -> list[tuple[float, float]]:
    """筛出有效的 correctness 评分，并把来源映射成权重。"""
    return [
        (score.score, policy.source_weights.get(score.source, 0.5))
        for score in scores
        if score.dimension == EvalDimension.CORRECTNESS.value and 0.0 <= score.score <= 1.0
    ]


def _weighted_shrink(
    samples: list[tuple[float, float]], policy: HealthPolicy
) -> tuple[float, float, float]:
    """加权均值后向中性先验收缩，返回 (raw, shrunk, confidence)。

    公式：raw = Σ(v·w)/Σw；shrunk = (raw·W + prior·W0)/(W + W0)，W0>0 时置信度 = W/(W+W0)。
    """
    sample_weight = sum(w for _, w in samples)
    prior_weight = policy.quality_prior_weight
    raw = sum(v * w for v, w in samples) / sample_weight
    shrunk = (raw * sample_weight + policy.quality_prior * prior_weight) / (
        sample_weight + prior_weight
    )
    confidence = sample_weight / (sample_weight + prior_weight) if prior_weight > 0 else 1.0
    return raw, shrunk, confidence


def _empty_quality(weight: float, note: str) -> DimensionScore:
    """没有样本时的占位维度：value=None，不参与聚合。"""
    return DimensionScore(
        key="quality",
        label=DIMENSION_LABELS["quality"],
        value=None,
        weight=weight,
        confidence=0.0,
        note=note,
        items=(ScoreItem("样本", 0.0, "0 条评分"),),
    )


def _quality_items(
    samples: list[tuple[float, float]], policy: HealthPolicy, raw: float, shrunk: float
) -> tuple[ScoreItem, ...]:
    """解释项：样本规模、原始均值、先验把分数拉动了多少。"""
    sample_weight = sum(w for _, w in samples)
    return (
        ScoreItem("样本", 0.0, f"{len(samples)} 条评分 · 权重和 {sample_weight:.1f}"),
        ScoreItem("原始均值", 0.0, f"{raw:.2f}"),
        ScoreItem(
            "先验收缩",
            _round((shrunk - raw) * 100.0),
            f"{raw:.2f} → {shrunk:.2f}（先验 {policy.quality_prior:.2f}）",
        ),
    )


def quality_detail(
    scores: list[EvalScoreRecord], policy: HealthPolicy, weight: float = 0.25
) -> DimensionScore:
    """效果质量：correctness 评分按来源加权，并向中性先验收缩。"""
    # 1. 只保留落在 0-1 的有效 correctness 评分，来源决定样本权重
    samples = _correctness_samples(scores, policy)
    # 2. 没有样本就给空维度，避免无证据的分数参与加权
    if not samples:
        return _empty_quality(weight, "没有 correctness 评分，该维度不参与加权")
    # 3. 加权求均值，再向先验收缩（详见 _weighted_shrink）
    raw, shrunk, confidence = _weighted_shrink(samples, policy)
    # 4. 归一化到 0-100 并保留解释项
    return DimensionScore(
        key="quality",
        label=DIMENSION_LABELS["quality"],
        value=_round(_clamp(shrunk * 100.0)),
        weight=weight,
        confidence=_round(confidence, 3),
        note=f"{len(samples)} 条评分",
        items=_quality_items(samples, policy, raw, shrunk),
    )


def safety_detail(spans: list[SpanRecord], weight: float = 0.15) -> DimensionScore:
    """安全合规：拒绝占比扣分，重复越界再追加。"""
    # 1. 只看权限判定 span；没有任何判定时给满分但压低置信度
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
    # 2. 拒绝率线性扣分（每次被拒封顶 60 分），重复越界每次再扣 5 分、最多 15 分
    denied = [span for span in permission if span.status == "error"]
    rate = len(denied) / len(permission)
    penalty = 60.0 * rate
    repeat = min(15.0, max(0, len(denied) - 1) * 5.0)
    value = _clamp(100.0 - penalty - repeat)
    # 3. 扣分明细与 note 只描述证据，不参与加权
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
