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

本包是纯函数层：只吃数据模型，不碰 sqlite、不 import agentscope。
实现按职责拆在 `model` / `components` / `quality` / `report` / `aggregate`，
此文件只做门面 re-export，对外接口与原单文件模块保持一致。
"""

from __future__ import annotations

from devops_agent.observability.health.aggregate import (
    aggregate,
    deficits,
    dimension_averages,
    distribution,
    grade_counts,
    percentile,
    summarize,
)
from devops_agent.observability.health.components import (
    efficiency_detail,
    execution_detail,
    latency_budget,
)
from devops_agent.observability.health.model import (
    DIMENSION_LABELS,
    DIMENSIONS,
    SCORING_VERSION,
    DimensionScore,
    HealthReport,
    ScoreItem,
)
from devops_agent.observability.health.quality import quality_detail, safety_detail
from devops_agent.observability.health.report import (
    efficiency_score,
    execution_score,
    grade_for,
    quality_score,
    report_run,
    safety_score,
    score_run,
)

__all__ = [
    "DIMENSIONS",
    "DIMENSION_LABELS",
    "SCORING_VERSION",
    "DimensionScore",
    "HealthReport",
    "ScoreItem",
    "aggregate",
    "deficits",
    "dimension_averages",
    "distribution",
    "efficiency_detail",
    "efficiency_score",
    "execution_detail",
    "execution_score",
    "grade_counts",
    "grade_for",
    "latency_budget",
    "percentile",
    "quality_detail",
    "quality_score",
    "report_run",
    "safety_detail",
    "safety_score",
    "score_run",
    "summarize",
]
