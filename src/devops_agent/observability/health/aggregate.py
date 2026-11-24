"""批量聚合 —— 看板与告警用的均值、百分位、分布与缺口。"""

from __future__ import annotations

from collections.abc import Sequence
from math import ceil

from devops_agent.config import HealthPolicy
from devops_agent.observability.health.model import (
    DIMENSION_LABELS,
    DIMENSIONS,
    HealthReport,
    _round,
)
from devops_agent.observability.models import RunRecord


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
