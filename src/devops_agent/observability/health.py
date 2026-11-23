"""健康分计算 —— 把一次运行的四类信号聚合成 0-100 的单一分数。

四类信号（ADR-0001）：
- execution：status 成功 / 失败，是否未收敛、工具失败；
- efficiency：端到端延迟（相对配置基准）、token 用量；
- quality：correctness / 人工反馈评分（缺失时忽略该维度并重归一化权重）；
- safety：权限拒绝 span 的占比。

本模块是纯函数层：只吃数据模型，不碰 sqlite、不 import agentscope。
"""

from __future__ import annotations

from math import ceil

from devops_agent.config import HealthPolicy
from devops_agent.observability.models import EvalScoreRecord, RunRecord, SpanRecord

# 评分维度 key（也是对外输出的子分名称）
DIMENSIONS = ("execution", "efficiency", "quality", "safety")


def _clamp(value: float) -> float:
    """限制到 0-100。"""
    return max(0.0, min(100.0, value))


def execution_score(run: RunRecord, spans: list[SpanRecord]) -> float:
    """执行健康：成功 100 / 失败 0，再按失败 span 与未收敛扣分。"""
    base = 100.0 if run.status == "success" else 0.0
    failures = sum(1 for s in spans if s.status == "error")
    penalty = min(40.0, failures * 10.0)
    if run.status == "failed":
        penalty = 0.0  # 已判失败不再重复扣，避免双重惩罚
    return _clamp(base - penalty)


def efficiency_score(run: RunRecord, baseline_ms: int) -> float:
    """效率：耗时 ≤ 基准得满分，超出按线性衰减到 0。"""
    duration = run.duration_ms
    if duration is None or duration <= 0:
        return 100.0
    if duration <= baseline_ms:
        return 100.0
    decay = 1.0 - (duration - baseline_ms) / baseline_ms
    return _clamp(decay * 100.0)


def quality_score(scores: list[EvalScoreRecord]) -> float | None:
    """效果质量：correctness 维度均值；无评分返回 None（该维度不参与加权）。"""
    values = [s.score for s in scores if s.dimension == "correctness" and 0.0 <= s.score <= 1.0]
    if not values:
        return None
    return _clamp(sum(values) / len(values) * 100.0)


def safety_score(spans: list[SpanRecord]) -> float:
    """安全合规：无权限拒绝 span 得满分，有则按占比扣分。"""
    permission = [s for s in spans if s.type == "permission"]
    if not permission:
        return 100.0
    denied = sum(1 for s in permission if s.status == "error")
    return _clamp(100.0 - denied / len(permission) * 100.0)


def score_run(
    run: RunRecord,
    spans: list[SpanRecord],
    scores: list[EvalScoreRecord],
    policy: HealthPolicy,
) -> float:
    """聚合四类子分为 0-100 健康分；缺失维度剔除后重归一化权重。"""
    sub: dict[str, float | None] = {
        "execution": execution_score(run, spans),
        "efficiency": efficiency_score(run, policy.latency_baseline_ms),
        "quality": quality_score(scores),
        "safety": safety_score(spans),
    }
    weights = policy.weights.model_dump()
    pairs: list[tuple[float, float]] = []
    for dim in DIMENSIONS:
        value = sub[dim]
        if value is not None:
            pairs.append((float(weights[dim]), value))
    total_weight = sum(w for w, _ in pairs)
    if total_weight <= 0:
        return 0.0
    return round(sum(w * v for w, v in pairs) / total_weight, 1)


def aggregate(sub_scores: list[float]) -> float | None:
    """对多条运行的分求均值；空列表返回 None。"""
    if not sub_scores:
        return None
    return round(sum(sub_scores) / len(sub_scores), 1)


def percentile(values: list[int], pct: float) -> int | None:
    """最近秩百分位；空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = ceil(len(ordered) * pct)  # 最近秩法：ceil(pct*n)，第 rank 个（1-based）
    index = max(0, min(len(ordered) - 1, rank - 1))
    return ordered[index]


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
