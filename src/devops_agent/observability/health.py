"""健康分计算 —— 把一次运行的四类信号聚合成 0-100 的单一分数（口径 v2）。

四类信号（ADR-0001 / ADR-0004）：

- execution：运行状态、工具失败、未收敛；
- efficiency：干活耗时相对「按步数给的预算」、单次调用的 token 强度；
- quality：correctness 评分按来源可信度加权，并向中性先验收缩；
- safety：权限拒绝占比与重复越界。

v2 相对 v1 的四点变化，都是为了能在生产里长期用：

1. **不再一刀切**：耗时缺失给 None（缺证据不等于满分），超预算按幂律衰减而不是线性归零；
2. **按规模归一**：延迟预算 = 首步基准 + 每次模型调用叠加，长会话不再天生 0 分；
3. **可解释**：每个子分带 `items` 明细（哪一条扣了几分），前端与告警都能说清原因；
4. **带置信度与覆盖度**：缺数据的维度不假装有分，聚合时剔除并记录 coverage / confidence。

本模块是纯函数层：只吃数据模型，不碰 sqlite、不 import agentscope。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import ceil

from devops_agent.config import HealthPolicy
from devops_agent.eval.scorer import EvalDimension
from devops_agent.observability.models import EvalScoreRecord, RunRecord, SpanRecord

# 评分口径版本：算法或默认配置变化时递增，便于解释「历史分数为什么不可直接比」
SCORING_VERSION = "v2"

# 评分维度 key（也是对外输出的子分名称）
DIMENSIONS = ("execution", "efficiency", "quality", "safety")

# 维度中文名：前端与告警文案共用一份，避免各处自己翻译
DIMENSION_LABELS: dict[str, str] = {
    "execution": "执行健康",
    "efficiency": "效率成本",
    "quality": "效果质量",
    "safety": "安全合规",
}

# 未收敛的判据：框架到达 max_iterations 时会把提示写进 run.error
_UNCONVERGED_MARKERS = ("max iteration", "max_iterations", "iteration numbers")

# 单个工具失败最多扣多少分：避免一次批量失败把执行健康打到 0 以下
_TOOL_FAIL_CAP = 24.0
# 工具失败率高于该阈值后再按比例追加扣分（偶发单点失败不该重罚）
_TOOL_RATE_FREE = 0.2
_TOOL_RATE_CAP = 18.0
# 未收敛额外扣分：跑满轮次说明任务没做完，即使没报错也要计
_UNCONVERGED_PENALTY = 15.0


def _clamp(value: float) -> float:
    """限制到 0-100。"""
    return max(0.0, min(100.0, value))


def _round(value: float, digits: int = 1) -> float:
    """统一保留位数：同一份数据重算必须得到同一个数。"""
    return round(value, digits)


@dataclass(frozen=True, slots=True)
class ScoreItem:
    """一条扣分/加分项：回答「这 12 分扣在哪」。"""

    label: str
    delta: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DimensionScore:
    """单个维度的得分与解释。value 为 None 表示证据不足、不参与聚合。"""

    key: str
    label: str
    value: float | None
    weight: float
    confidence: float
    note: str
    items: tuple[ScoreItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """一次运行的完整评分报告。"""

    score: float
    grade: str
    coverage: float
    confidence: float
    version: str
    dimensions: dict[str, DimensionScore]

    def as_dict(self) -> dict[str, object]:
        """转成 JSON 友好的结构（API 与前端共用）。"""
        return {
            "score": self.score,
            "grade": self.grade,
            "coverage": self.coverage,
            "confidence": self.confidence,
            "version": self.version,
            "dimensions": {
                key: {
                    "key": dim.key,
                    "label": dim.label,
                    "value": dim.value,
                    "weight": dim.weight,
                    "confidence": dim.confidence,
                    "note": dim.note,
                    "items": [
                        {"label": item.label, "delta": item.delta, "detail": item.detail}
                        for item in dim.items
                    ],
                }
                for key, dim in self.dimensions.items()
            },
        }


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


# ---- 批量聚合：看板与告警用 ---------------------------------------------


def aggregate(sub_scores: list[float]) -> float | None:
    """对多条运行的分求均值；空列表返回 None。"""
    if not sub_scores:
        return None
    return _round(sum(sub_scores) / len(sub_scores))


def percentile(values: Sequence[float], pct: float) -> float | None:
    """最近秩百分位；空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = ceil(len(ordered) * pct)  # 最近秩法：ceil(pct*n)，第 rank 个（1-based）
    index = max(0, min(len(ordered) - 1, rank - 1))
    return ordered[index]


def distribution(values: list[float], buckets: int = 10) -> list[dict[str, object]]:
    """把 0-100 的分值切成等宽桶，返回直方图（100 归入最后一桶）。"""
    counts = [0] * buckets
    width = 100.0 / buckets
    for value in values:
        index = min(buckets - 1, max(0, int(value // width)))
        counts[index] += 1
    return [
        {
            "bucket": f"{int(i * width)}-{int((i + 1) * width) if i < buckets - 1 else 100}",
            "from": int(i * width),
            "count": counts[i],
        }
        for i in range(buckets)
    ]


def dimension_averages(reports: list[HealthReport]) -> dict[str, dict[str, object]]:
    """各维度均值与样本数：样本数决定这条均值该信几分。"""
    result: dict[str, dict[str, object]] = {}
    for key in DIMENSIONS:
        values: list[float] = []
        for report in reports:
            value = report.dimensions[key].value
            if value is not None:
                values.append(value)
        result[key] = {
            "label": DIMENSION_LABELS[key],
            "avg": aggregate(values),
            "samples": len(values),
        }
    return result


def deficits(
    averages: dict[str, dict[str, object]], policy: HealthPolicy
) -> list[dict[str, object]]:
    """各维度平均「拖了多少分」：weight × (100 − 均值)，从大到小。

    比单纯看均值更能回答「先修哪个」——权重高的维度掉一点就很贵。
    """
    weights = policy.weights.model_dump()
    rows: list[tuple[float, dict[str, object]]] = []
    for key in DIMENSIONS:
        raw = averages.get(key, {}).get("avg")
        if not isinstance(raw, (int, float)):
            continue
        avg = float(raw)
        loss = float(weights[key]) * (100.0 - avg)
        rows.append(
            (
                loss,
                {
                    "key": key,
                    "label": DIMENSION_LABELS[key],
                    "avg": _round(avg),
                    "loss": _round(loss),
                },
            )
        )
    return [row for _, row in sorted(rows, key=lambda item: item[0], reverse=True)]


def grade_counts(reports: list[HealthReport]) -> dict[str, int]:
    """等级分布：A/B/C/D/F 各多少次。"""
    counts: dict[str, int] = {}
    for report in reports:
        counts[report.grade] = counts.get(report.grade, 0) + 1
    return dict(sorted(counts.items()))


def _count_status(runs: list[RunRecord], status: str) -> int:
    """统计指定状态的 run 数。"""
    return sum(1 for r in runs if r.status == status)


def _rounded_ratio(num: int, den: int) -> float | None:
    """安全除法并保留 4 位，分母为 0 返回 None。"""
    return round(num / den, 4) if den else None


def summarize(runs: list[RunRecord]) -> dict[str, object]:
    """从一批 run 汇总：总数、成功/失败数、成功率、平均健康分、P95 延迟、token、成本代理值。"""
    total = len(runs)
    succeeded = _count_status(runs, "success")
    failed = _count_status(runs, "failed")
    durations = [r.duration_ms for r in runs if r.duration_ms is not None]
    health_values = [r.health_score for r in runs if r.health_score is not None]
    input_tokens = sum(r.input_tokens for r in runs)
    output_tokens = sum(r.output_tokens for r in runs)
    return {
        "total_runs": total,
        "success_count": succeeded,
        "failed_count": failed,
        "success_rate": _rounded_ratio(succeeded, total),
        "avg_health_score": aggregate(health_values),
        "p95_duration_ms": percentile(durations, 0.95),
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        # 成本代理值：真实单价按模型区分，这里给出 token 总量供前端按价换算
        "token_proxy": input_tokens + output_tokens,
    }
