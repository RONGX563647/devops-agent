"""短期（工作记忆）的确定性裁剪：不调模型，只做头尾保留、去重与旧轮次淘汰。

只改本次请求的 messages 副本；当前用户输入与最近一轮永远保留。
快照前可复用同一套规则收缩 state.context（见 `trim_context_for_snapshot`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentscope.message import Msg

from devops_agent.memory.tokens import estimate_tokens
from devops_agent.memory_policy import ShortTermPolicy

_OMITTED = "[重复读取已省略]"
_MIN_TOOL_CHARS = 200


@dataclass
class TrimReport:
    """裁剪结果：丢弃类别与回收字符，直接进 MEMORY_TRIM 事件。"""

    dropped_tool_results: int = 0
    deduped_reads: int = 0
    dropped_turns: int = 0
    reclaimed_chars: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.dropped_tool_results or self.deduped_reads or self.dropped_turns)

    def as_dict(self) -> dict[str, int]:
        return {
            "dropped_tool_results": self.dropped_tool_results,
            "deduped_reads": self.deduped_reads,
            "dropped_turns": self.dropped_turns,
            "reclaimed_chars": self.reclaimed_chars,
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


def _truncate_blocks(msg: Msg, policy: ShortTermPolicy, report: TrimReport) -> Msg:
    """把单条消息里超长工具结果换成头尾保留版；无改动返回原对象。"""
    if not isinstance(msg.content, list):
        return msg
    blocks: list[Any] = []
    touched = False
    for block in msg.content:
        if not _is_tool_result(block):
            blocks.append(block)
            continue
        raw = _output_text(getattr(block, "output", ""))
        shortened = truncate_output(raw, policy.tool_output_chars, policy.tool_output_head_ratio)
        if shortened == raw:
            blocks.append(block)
            continue
        touched = True
        report.dropped_tool_results += 1
        report.reclaimed_chars += len(raw) - len(shortened)
        blocks.append(block.model_copy(update={"output": shortened}))
    return _with_blocks(msg, blocks) if touched else msg


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
    return [
        index
        for index, msg in enumerate(messages)
        if msg.role == "user" and msg.name not in ("system", "user")
    ]


def _drop_old_turns(messages: list[Msg], max_turns: int, report: TrimReport) -> list[Msg]:
    starts = _turn_starts(messages)
    if len(starts) <= max_turns:
        return messages
    keep_from = starts[len(starts) - max_turns]
    head = messages[: starts[0]]
    report.dropped_turns += len(starts) - max_turns
    return head + messages[keep_from:]


def _token_size(messages: list[Msg]) -> int:
    return sum(estimate_tokens(_output_text(msg.model_dump(mode="json"))) for msg in messages)


def trim_messages(
    messages: list[Msg],
    policy: ShortTermPolicy,
    *,
    max_tokens: int | None = None,
) -> tuple[list[Msg], TrimReport]:
    """对请求 messages 做一次确定性裁剪；返回新列表与统计。

    顺序：工具结果头尾保留 → 重复读取去重 → 淘汰最旧轮次 → 按 token 预算淘汰。
    最后一步只在调用方给出 max_tokens 时执行，让短期面真正吃上下文预算，
    而不是只按轮数裁。
    """
    report = TrimReport()
    # 去重必须看原文：先截断会让「同一份输出」变成同一段头尾，反而难以识别
    trimmed = list(messages)
    if policy.dedup_reads:
        trimmed = _dedupe_reads(trimmed, report)
    trimmed = [_truncate_blocks(msg, policy, report) for msg in trimmed]
    trimmed = _drop_old_turns(trimmed, policy.max_turns, report)
    if max_tokens is not None:
        # 预算只算历史面：系统提示/档位说明等头部已由 prefix 从预算里扣过
        while True:
            starts = _turn_starts(trimmed)
            if len(starts) <= 1 or _token_size(trimmed[starts[0] :]) <= max_tokens:
                break
            dropped = trimmed[starts[0] : starts[1]]
            report.dropped_turns += 1
            report.reclaimed_chars += _size(dropped)
            trimmed = trimmed[: starts[0]] + trimmed[starts[1] :]
    return trimmed, report


def _size(messages: list[Msg]) -> int:
    return len(json.dumps([msg.model_dump(mode="json") for msg in messages], default=str))


def trim_context_for_snapshot(
    messages: list[Msg], policy: ShortTermPolicy, max_chars: int
) -> tuple[list[Msg], TrimReport]:
    """快照前的体积保护：先常规裁剪，再按字符上限继续丢最旧轮次。

    整体大小只算一次，之后按「丢掉的那段」递减，避免每轮都全量序列化。
    """
    trimmed, report = trim_messages(messages, policy)
    total = _size(trimmed)
    while len(_turn_starts(trimmed)) > 1 and total > max_chars:
        starts = _turn_starts(trimmed)
        dropped = trimmed[starts[0] : starts[1]]
        dropped_size = _size(dropped)
        report.dropped_turns += 1
        report.reclaimed_chars += dropped_size
        trimmed = trimmed[: starts[0]] + trimmed[starts[1] :]
        total -= dropped_size
    return trimmed, report
