"""可观测性数据模型 —— 纯数据结构，不依赖任何框架。

三个模型对应三张表：RunRecord（一次运行）、SpanRecord（运行中的一个步骤）、
EvalScoreRecord（一次质量打分）。字段设计与 ADR-0001 的数据模型一致。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一次运行的完整记录。"""

    id: int | None
    session_id: str
    domain: str
    status: str  # running / success / failed
    input: str
    output: str
    started_at: str
    ended_at: str | None
    duration_ms: int | None
    model: str
    total_tokens: int
    health_score: float | None


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """运行中的一个步骤（模型调用 / 工具调用 / 权限判定）。"""

    id: int | None
    run_id: int
    type: str  # model / tool / permission
    name: str
    start_offset_ms: int
    duration_ms: int | None
    input: str
    output: str
    tokens: int
    status: str  # ok / error
    error: str | None


@dataclass(frozen=True, slots=True)
class EvalScoreRecord:
    """一次质量评分（人工或自动）。"""

    id: int | None
    run_id: int
    dimension: str  # correctness / performance / safety
    score: float
    source: str  # human / judge / rule
    rater: str
    created_at: str
    comment: str
