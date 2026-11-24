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
    # P1 补充：输入输出 token 分拆，用于按不同单价算成本
    input_tokens: int = 0
    output_tokens: int = 0
    # 前缀缓存命中量（input_tokens 已含这部分）与本次新写入缓存的量。
    # 只记 input/output 时算不出命中率，界面上就只剩一个拍脑袋的百分比。
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # P1 补充：客户端幂等键，防止重试写入重复 run
    request_id: str = ""
    # 本轮的权限档位（readonly / workspace / execute / unrestricted / auto）
    permission: str = ""
    # 两个耗时口径：work_ms = span 并集（真正在干活的时间），
    # wall_ms = 起止差（含用户读消息/走开的空闲）。只留一个数会永远说不清差在哪。
    work_ms: int = 0
    wall_ms: int = 0
    # 失败原因原文。之前 status=failed 只是个标签：span 全绿、输出完整，
    # 却没有任何地方留下异常信息，刷新后连「为什么失败」都查不到。
    error: str = ""
    # 本轮实际下发的模型参数（JSON，只含非默认值）。空串 = 老记录/未记录，
    # 读取方要显示「参数未知」而不是按默认值解释 —— 那会造出假的归因。
    model_params: str = ""


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
    # P1 补充：模型调用的 token 分拆，工具 span 保持 0
    input_tokens: int = 0
    output_tokens: int = 0
    # 这一次调用的缓存命中/写入量：逐调用可见，才回答得了「哪一步开始全冷」
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # run 内单调序号：泳道与列表按同一顺序排，才不会再出现「泳道有、列表无」。
    # 老记录为 0，读取时退化为按 start_offset_ms 排序。
    seq: int = 0
    # 发起这次调用的模型 span id（工具 → 模型）：回答得了「哪个模型轮次调了它」。
    parent_id: int | None = None


@dataclass(frozen=True, slots=True)
class InputStageRecord:
    """输入解析的一个阶段。

    与 SpanRecord 分开存：解析发生在调用 Agent 之前，不是 run 内的一个 span，
    放进 spans 会把「模型/工具/权限」的语义搞混，也会污染健康分口径。
    """

    id: int | None
    run_id: int
    seq: int
    stage: str
    label: str
    status: str  # ok / skip / error
    changed: bool
    summary: str
    before: int
    after: int
    items: int
    duration_ms: int
    detail: str
    failed: int = 0


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
