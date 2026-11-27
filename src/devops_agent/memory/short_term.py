"""短期（工作记忆）的确定性裁剪：不调模型，只做头尾保留、去重与旧轮次淘汰。

只改本次请求的 messages 副本；当前用户输入与最近一轮永远保留。
快照前可复用同一套规则收缩 state.context（见 `trim_context_for_snapshot`）。

SPEC-0016 之后有两个变化：

1. **预算是硬约束**（`max_tokens`）：先量，不超就原样返回同一个 list 对象。
   这是前缀缓存的第一条不变量 —— 未超预算的轮次改一个字节都是净损失。
2. **去重默认只在超预算时做**（`dedup_reads: on_pressure`）：旧默认每轮都改写
   历史中段，把「从该点起的整段缓存」全部作废，而多数轮次根本没超预算。

五阶段顺序按「改动点从后往前」排：尾段工具输出 → 去重 → 中段截断 → 淘汰旧轮次
→ 收紧当前轮。越靠前的阶段动的位置越靠后，缓存失效的长度越短。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentscope.message import Msg

from devops_agent.memory.tokens import count_messages
from devops_agent.memory.turns import is_user_turn
from devops_agent.memory_policy import ShortTermPolicy

_OMITTED = "[重复读取已省略]"
_MIN_TOOL_CHARS = 200
#: S5 的收紧倍率：单轮本身过大时，把当前轮的工具输出砍到常规上限的四分之一
_TIGHT_DIVISOR = 4
_TIGHT_FLOOR = 80

#: 阶段 → `history_changed_by`：让「缓存为什么失效」在轨迹里可归因
STAGE_REASON = {
    "S1": "trim_tail",
    "S2": "dedup",
    "S3": "trim_mid",
    "S4": "drop_turns",
    "S5": "trim_tail",
}


@dataclass
class TrimReport:
    """裁剪结果：丢弃类别、回收字符与预算口径，直接进 MEMORY_TRIM 事件。"""

    dropped_tool_results: int = 0
    deduped_reads: int = 0
    dropped_turns: int = 0
    reclaimed_chars: int = 0
    #: 预算口径（SPEC-0016 §5.2 A34）：没有预算时三项保持 0
    tokens_before: int = 0
    tokens_after: int = 0
    limit: int = 0
    over_budget: bool = False
    stage: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.dropped_tool_results or self.deduped_reads or self.dropped_turns)

    @property
    def changes(self) -> int:
        """改动计数，用来判断某一阶段是否真的动了东西。"""
        return self.dropped_tool_results + self.deduped_reads + self.dropped_turns

    @property
    def changed_by(self) -> str:
        """缓存失效归因；未改动为 `none`。"""
        return STAGE_REASON.get(self.stage, "none")

    def as_dict(self) -> dict[str, Any]:
        return {
            "dropped_tool_results": self.dropped_tool_results,
            "deduped_reads": self.deduped_reads,
            "dropped_turns": self.dropped_turns,
            "reclaimed_chars": self.reclaimed_chars,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "limit": self.limit,
            "over_budget": self.over_budget,
            "stage": self.stage,
        }


def truncate_output(text: str, limit: int, head_ratio: float) -> str:
    """保留头部 head_ratio、尾部其余，中间标注截断字数。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(1, int(limit * head_ratio))
    tail = max(1, limit - head)
    omitted = len(text) - head - tail
    if omitted <= 0:
        return text
    return f"{text[:head]}\n…[已截断 {omitted} 字]…\n{text[-tail:]}"


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, default=str)


def _is_tool_result(block: Any) -> bool:
    return str(getattr(block, "type", "")) == "tool_result"


def _with_blocks(msg: Msg, blocks: list[Any]) -> Msg:
    return msg.model_copy(update={"content": blocks})


def _truncate_blocks(
    msg: Msg, policy: ShortTermPolicy, report: TrimReport, limit: int | None = None
) -> Msg:
    """把单条消息里超长工具结果换成头尾保留版；无改动返回原对象。"""
    if not isinstance(msg.content, list):
        return msg
    cap = policy.tool_output_chars if limit is None else limit
    blocks: list[Any] = []
    touched = False
    for block in msg.content:
        if not _is_tool_result(block):
            blocks.append(block)
            continue
        raw = _output_text(getattr(block, "output", ""))
        shortened = truncate_output(raw, cap, policy.tool_output_head_ratio)
        if shortened == raw:
            blocks.append(block)
            continue
        touched = True
        report.dropped_tool_results += 1
        report.reclaimed_chars += len(raw) - len(shortened)
        blocks.append(block.model_copy(update={"output": shortened}))
    return _with_blocks(msg, blocks) if touched else msg


def _truncate_slice(
    messages: list[Msg],
    start: int,
    end: int,
    policy: ShortTermPolicy,
    report: TrimReport,
    limit: int | None = None,
) -> list[Msg]:
    """只改 [start, end) 这一段；段外的消息对象原样保留（缓存不受影响）。"""
    out = list(messages)
    for index in range(max(0, start), min(end, len(out))):
        out[index] = _truncate_blocks(out[index], policy, report, limit)
    return out


def _read_keys(messages: list[Msg]) -> dict[tuple[str, int], int]:
    """记录每份工具返回最后出现的位置；短输出不参与（信息量太小）。"""
    seen: dict[tuple[str, int], int] = {}
    for index, msg in enumerate(messages):
        for block in msg.content if isinstance(msg.content, list) else []:
            if not _is_tool_result(block):
                continue
            text = _output_text(getattr(block, "output", ""))
            if len(text) >= _MIN_TOOL_CHARS:
                seen[(str(getattr(block, "name", "")), hash(text))] = index
    return seen


def _dedupe_message(
    msg: Msg, index: int, seen: dict[tuple[str, int], int], report: TrimReport
) -> Msg | None:
    """把非最后出现的重复读取替换成占位；没有改动返回 None。"""
    if not isinstance(msg.content, list):
        return None
    blocks: list[Any] = []
    touched = False
    for block in msg.content:
        if not _is_tool_result(block):
            blocks.append(block)
            continue
        text = _output_text(getattr(block, "output", ""))
        key = (str(getattr(block, "name", "")), hash(text))
        if len(text) >= _MIN_TOOL_CHARS and seen.get(key) != index:
            report.deduped_reads += 1
            report.reclaimed_chars += max(0, len(text) - len(_OMITTED))
            blocks.append(block.model_copy(update={"output": _OMITTED}))
            touched = True
        else:
            blocks.append(block)
    return _with_blocks(msg, blocks) if touched else None


def _dedupe_reads(messages: list[Msg], report: TrimReport) -> list[Msg]:
    """同一份工具返回只留最后一次：中间那些重复读取只占窗口不提供新信息。"""
    seen = _read_keys(messages)
    out = list(messages)
    for index, msg in enumerate(messages):
        replaced = _dedupe_message(msg, index, seen, report)
        if replaced is not None:
            out[index] = replaced
    return out


def _turn_starts(messages: list[Msg]) -> list[int]:
    """用户输入消息的位置；系统注脚与框架摘要不算一轮的开始。"""
    return [index for index, msg in enumerate(messages) if is_user_turn(msg)]


def _last_user_index(messages: list[Msg]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if is_user_turn(messages[index]):
            return index
    return None


def _drop_old_turns(messages: list[Msg], max_turns: int, report: TrimReport) -> list[Msg]:
    starts = _turn_starts(messages)
    if len(starts) <= max_turns:
        return messages
    keep_from = starts[len(starts) - max_turns]
    head = messages[: starts[0]]
    report.dropped_turns += len(starts) - max_turns
    return head + messages[keep_from:]


def _dedup_enabled(policy: ShortTermPolicy, override: str | None) -> bool:
    """`on_pressure` 走到这里时已经确认超预算（S0 早退在前），所以等价于启用。

    没有预算的手工 / 快照调用无法判断压力，按「尽量省」处理：快照不是每轮请求，
    改它不影响前缀缓存。
    """
    mode = override or policy.dedup_reads
    return mode != "never"


def _stage_tail(
    messages: list[Msg], policy: ShortTermPolicy, report: TrimReport, limit: int
) -> list[Msg]:
    """S1：先动最后一条 user 之后的工具输出 —— 改动点最靠后，废的缓存最短。"""
    tail = _last_user_index(messages)
    if tail is None:
        return messages
    return _truncate_slice(messages, tail, len(messages), policy, report)


def _stage_dedup(
    messages: list[Msg], policy: ShortTermPolicy, report: TrimReport, limit: int
) -> list[Msg]:
    """S2：去重必须看原文，所以排在截断之前。"""
    if not _dedup_enabled(policy, None):
        return messages
    return _dedupe_reads(messages, report)


def _stage_mid(
    messages: list[Msg], policy: ShortTermPolicy, report: TrimReport, limit: int
) -> list[Msg]:
    """S3：历史中段的超长工具输出。"""
    tail = _last_user_index(messages)
    end = len(messages) if tail is None else tail
    return _truncate_slice(messages, 0, end, policy, report)


def _stage_drop_turns(
    messages: list[Msg], policy: ShortTermPolicy, report: TrimReport, limit: int
) -> list[Msg]:
    """S4：从最旧整轮开始淘汰，直到装得下或只剩一轮。

    只切整轮（`head + messages[keep_from:]`）：轮内挖洞会让「工具调用」与
    「它的结果」错位，模型看到没有结果的调用会重试。
    """
    current = messages
    while len(_turn_starts(current)) > 1 and count_messages(current) > limit:
        current = _drop_old_turns(current, len(_turn_starts(current)) - 1, report)
    return current


def _stage_tight(
    messages: list[Msg], policy: ShortTermPolicy, report: TrimReport, limit: int
) -> list[Msg]:
    """S5：单轮本身过大 —— 只砍当前轮的工具输出，用户输入永不截断。"""
    tail = _last_user_index(messages)
    start = 0 if tail is None else tail
    tight = max(_TIGHT_FLOOR, policy.tool_output_chars // _TIGHT_DIVISOR)
    return _truncate_slice(messages, start, len(messages), policy, report, tight)


#: 顺序即算法：每个阶段接收 (messages, policy, report, limit) 并返回新列表
_STAGES: tuple[tuple[str, Callable[..., list[Msg]]], ...] = (
    ("S1", _stage_tail),
    ("S2", _stage_dedup),
    ("S3", _stage_mid),
    ("S4", _stage_drop_turns),
    ("S5", _stage_tight),
)


def trim_messages(
    messages: list[Msg],
    policy: ShortTermPolicy,
    max_tokens: int | None = None,
    *,
    dedup: str | None = None,
) -> tuple[list[Msg], TrimReport]:
    """对请求 messages 做一次确定性裁剪；返回新列表与统计。

    `max_tokens` 给定时进入预算执行模式：先量，不超就**返回同一个对象**
    （S0，前缀缓存的前提）；超了才按五阶段依次改，任一阶段后装得下就停。
    `max_tokens` 为 None 时是手工 / 快照调用，按「去重 → 截断 → 淘汰轮次」执行。
    """
    report = TrimReport(limit=max(0, max_tokens or 0))
    if max_tokens is None:
        trimmed = _dedupe_reads(messages, report) if _dedup_enabled(policy, dedup) else messages
        trimmed = _truncate_slice(trimmed, 0, len(trimmed), policy, report)
        return _drop_old_turns(trimmed, policy.max_turns, report), report

    report.tokens_before = count_messages(messages)
    if report.tokens_before <= max_tokens:
        report.tokens_after = report.tokens_before
        return messages, report

    current = list(messages)
    for stage, step in _STAGES:
        before = report.changes
        current = step(current, policy, report, max_tokens)
        if report.changes == before:
            continue
        report.stage = stage
        if count_messages(current) <= max_tokens:
            break
    report.tokens_after = count_messages(current)
    report.over_budget = report.tokens_after > max_tokens
    return current, report


def _size(messages: list[Msg]) -> int:
    return len(json.dumps([msg.model_dump(mode="json") for msg in messages], default=str))


def trim_context_for_snapshot(
    messages: list[Msg], policy: ShortTermPolicy, max_chars: int
) -> tuple[list[Msg], TrimReport]:
    """快照前的体积保护：先常规裁剪，再按字符上限继续丢最旧轮次。"""
    trimmed, report = trim_messages(messages, policy)
    while len(_turn_starts(trimmed)) > 1 and _size(trimmed) > max_chars:
        starts = _turn_starts(trimmed)
        dropped = trimmed[starts[0] : starts[1]]
        report.dropped_turns += 1
        report.reclaimed_chars += _size(dropped)
        trimmed = trimmed[: starts[0]] + trimmed[starts[1] :]
    return trimmed, report
