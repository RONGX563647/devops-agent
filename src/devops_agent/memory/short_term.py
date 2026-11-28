"""短期（工作记忆）的确定性裁剪：不调模型，只做头尾保留、去重与旧轮次淘汰。

三条边界按顺序生效（SPEC-0019 §3）：

- **S0 预算**：未超 `history_limit` 就零改动。改写历史只是白付前缀缓存，
  而且会把「本来装得下」的内容变成模型看不到；
- **S1 未消化**：尾部最近 N 个工具结果豁免内容截断。它们正是下一步推理的
  唯一依据；旧轮次的工具结果早已被总结成 assistant 文本（文本块不裁），
  按同一上限处理等于「已消化的留着、未消化的砍掉」；
- **S3 已消化**：更旧的工具结果才截断，切点落在行边界、标记带原行号区间，
  让模型能用一次 `offset/limit` 定向补读，而不是整份重读。

只改本次请求的 messages 副本。快照期只做「因体积丢整轮」，不复用常规裁剪：
常规裁剪是请求期行为，写回 `state.context` 就变成永久丢失（SPEC-0019 §5.2）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentscope.message import Msg

from devops_agent.memory.tokens import estimate_messages
from devops_agent.memory.turns import is_user_turn
from devops_agent.memory_policy import ShortTermPolicy

_OMITTED = "[重复读取已省略]"
_MIN_TOOL_CHARS = 200
# 读取类工具的默认整份返回（框架默认 offset=1 / limit=2000），用作区间缺省
_DEFAULT_READ_LINES = 2000
_READ_TARGET_KEYS = ("file_path", "path", "file")

#: 阶段 → `history_changed_by`：让「缓存为什么失效」在轨迹里可归因。
#: 阶段值由 `trim_messages` 结算（s0 早退、s2 去重、s3 砍旧结果、s4 淘汰轮次、
#: s5 收紧当前轮）；归因字符串是给前端与评测看的稳定词表。
STAGE_REASON = {
    "s1": "trim_tail",
    "s2": "dedup",
    "s3": "trim_mid",
    "s4": "drop_turns",
    "s5": "trim_tail",
}


@dataclass
class TrimReport:
    """裁剪结果：丢弃类别与回收字符，直接进 MEMORY_TRIM 事件。"""

    dropped_tool_results: int = 0
    deduped_reads: int = 0
    dropped_turns: int = 0
    reclaimed_chars: int = 0
    # 被豁免（完整保留）的工具结果数与「最深生效阶段」，用于区分「为什么没砍」
    exempt_results: int = 0
    truncated_lines: int = 0
    stage: str = ""
    #: 预算口径（SPEC-0016 §5.2 A34）：没有预算时三项保持 0
    tokens_before: int = 0
    tokens_after: int = 0
    limit: int = 0
    over_budget: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.dropped_tool_results or self.deduped_reads or self.dropped_turns)

    @property
    def changes(self) -> int:
        """改动计数，用来判断某一阶段是否真的动了东西。"""
        return self.dropped_tool_results + self.deduped_reads + self.dropped_turns

    @property
    def changed_by(self) -> str:
        """缓存失效归因；未改动（含 s0 早退）为 `none`。"""
        return STAGE_REASON.get(self.stage, "none") if self.changed else "none"

    def as_dict(self) -> dict[str, int | str]:
        return {
            "dropped_tool_results": self.dropped_tool_results,
            "deduped_reads": self.deduped_reads,
            "dropped_turns": self.dropped_turns,
            "reclaimed_chars": self.reclaimed_chars,
            "exempt_results": self.exempt_results,
            "truncated_lines": self.truncated_lines,
            "stage": self.stage,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "limit": self.limit,
            "over_budget": self.over_budget,
        }


def _split_at_lines(text: str, head: int, tail: int) -> tuple[str, str]:
    """把切点挪到行边界：头部退到最后一个换行，尾部前进到下一个换行。

    半行被切掉的代价不是「少几个字」，而是模型会把残缺的 `def f(self, x`
    当成语法错误去修。只有单行本身超长（退让超过 20%）才接受硬切。
    """
    head_end = head
    cut = text.rfind("\n", 0, head)
    if cut >= int(head * 0.8):
        head_end = cut + 1
    tail_start = len(text) - tail
    nxt = text.find("\n", tail_start)
    if 0 <= nxt < len(text) - 1:
        tail_start = nxt + 1
    return text[:head_end], text[tail_start:]


def truncate_output(text: str, limit: int, head_ratio: float) -> tuple[str, int]:
    """保留头部 head_ratio、尾部其余；返回 (新文本, 省略行数)。

    标记里必须写「原第 X–Y 行」而不只是字数：模型据此能算出 `offset`，
    把「整份重读」降级成一次定向读取。
    """
    if limit <= 0 or len(text) <= limit:
        return text, 0
    head = max(1, int(limit * head_ratio))
    tail = max(1, limit - head)
    if len(text) - head - tail <= 0:
        return text, 0
    head_text, tail_text = _split_at_lines(text, head, tail)
    omitted = len(text) - len(head_text) - len(tail_text)
    if omitted <= 0:
        return text, 0
    first = text.count("\n", 0, len(head_text)) + 1
    lines = text.count("\n", len(head_text), len(text) - len(tail_text))
    last = first + max(0, lines - 1)
    mark = (
        f"\n…[已截断 {omitted} 字 / {lines} 行（原第 {first}–{last} 行）"
        f"：重新读取时用 offset/limit 或更窄的过滤取中段]…\n"
    )
    return f"{head_text}{mark}{tail_text}", lines


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, default=str)


def _is_tool_result(block: Any) -> bool:
    return str(getattr(block, "type", "")) == "tool_result"


def _with_blocks(msg: Msg, blocks: list[Any]) -> Msg:
    return msg.model_copy(update={"content": blocks})


def _exempt_positions(messages: list[Msg], count: int) -> set[tuple[int, int]]:
    """尾部最近 `count` 个工具结果的位置；按「块」计数，不按消息。

    一次并行读 5 个文件是 5 个块：按消息计数会把它们当成一轮、只豁免一条。
    """
    if count <= 0:
        return set()
    found: set[tuple[int, int]] = set()
    remaining = count
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].content
        if not isinstance(content, list):
            continue
        for pos in range(len(content) - 1, -1, -1):
            if not _is_tool_result(content[pos]):
                continue
            found.add((index, pos))
            remaining -= 1
            if remaining <= 0:
                return found
    return found


def _truncate_blocks(
    msg: Msg,
    index: int,
    policy: ShortTermPolicy,
    report: TrimReport,
    exempt: set[tuple[int, int]],
    *,
    limit: int,
    only_exempt: bool,
) -> Msg:
    """截断单条消息里的工具结果；`only_exempt` 决定这次只动豁免块还是只动其余。

    S3 用 `only_exempt=False`（旧轮次按常规上限），S5 用 `True`（单轮本身超预算时
    连最新结果也要压，但用更宽的 `recent_output_chars`）。
    """
    if not isinstance(msg.content, list):
        return msg
    blocks: list[Any] = []
    touched = False
    for pos, block in enumerate(msg.content):
        if not _is_tool_result(block):
            blocks.append(block)
            continue
        if ((index, pos) in exempt) != only_exempt:
            blocks.append(block)
            continue
        raw = _output_text(getattr(block, "output", ""))
        shortened, lines = truncate_output(raw, limit, policy.tool_output_head_ratio)
        if shortened == raw:
            blocks.append(block)
            continue
        touched = True
        report.dropped_tool_results += 1
        report.truncated_lines += lines
        report.reclaimed_chars += len(raw) - len(shortened)
        blocks.append(block.model_copy(update={"output": shortened}))
    return _with_blocks(msg, blocks) if touched else msg


@dataclass(frozen=True, slots=True)
class _ReadSpan:
    """一次读取的目标与行区间（1-based，含端点）；解析不出时为 None。"""

    target: str
    start: int
    end: int


def _as_dict(raw: Any) -> dict[str, Any]:
    """工具入参在框架里是 JSON 字符串（不是 dict），解析失败按「拿不到」处理。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _call_inputs(messages: list[Msg]) -> dict[str, dict[str, Any]]:
    """`tool_call.id -> input`：工具结果本身不带入参，区间信息只能从调用块取。"""
    found: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if not isinstance(msg.content, list):
            continue
        for block in msg.content:
            if str(getattr(block, "type", "")) != "tool_call":
                continue
            call_id = str(getattr(block, "id", "") or "")
            data = _as_dict(getattr(block, "input", None))
            if call_id and data:
                found[call_id] = data
    return found


def _span_of(block: Any, inputs: dict[str, dict[str, Any]]) -> _ReadSpan | None:
    data = inputs.get(str(getattr(block, "id", "") or ""))
    if not data:
        return None
    target = next((str(data[key]) for key in _READ_TARGET_KEYS if data.get(key)), "")
    if not target:
        return None
    start = int(data.get("offset") or 1)
    limit = int(data.get("limit") or _DEFAULT_READ_LINES)
    return _ReadSpan(target, start, start + max(0, limit) - 1)


def _superseded_reads(
    messages: list[Msg], inputs: dict[str, dict[str, Any]]
) -> set[tuple[int, int]]:
    """被后来的读取整段覆盖的位置：同目标、后读区间包含前读区间。

    旧的键是 `(工具名, 全文哈希)`，只认「逐字节相同」的重复；同一文件先全量读、
    再分段读时两份内容都留着。按区间判定能把这类冗余也收掉，且不会误删
    「读的是不同段」的内容。
    """
    superseded: set[tuple[int, int]] = set()
    seen: dict[str, list[tuple[int, int, _ReadSpan]]] = {}
    for index, msg in enumerate(messages):
        if not isinstance(msg.content, list):
            continue
        for pos, block in enumerate(msg.content):
            if not _is_tool_result(block):
                continue
            span = _span_of(block, inputs)
            if span is None:
                continue
            for prev_index, prev_pos, prev in seen.get(span.target, []):
                if span.start <= prev.start and span.end >= prev.end:
                    superseded.add((prev_index, prev_pos))
            seen.setdefault(span.target, []).append((index, pos, span))
    return superseded


def _last_by_hash(messages: list[Msg]) -> dict[tuple[str, int], int]:
    """兜底去重：拿不到区间时，只认逐字节相同的返回；短输出不参与。"""
    seen: dict[tuple[str, int], int] = {}
    for index, msg in enumerate(messages):
        for block in msg.content if isinstance(msg.content, list) else []:
            if not _is_tool_result(block):
                continue
            text = _output_text(getattr(block, "output", ""))
            if len(text) >= _MIN_TOOL_CHARS:
                seen[(str(getattr(block, "name", "")), hash(text))] = index
    return seen


def _should_drop(
    block: Any,
    index: int,
    pos: int,
    hashes: dict[tuple[str, int], int],
    superseded: set[tuple[int, int]],
    exempt: set[tuple[int, int]],
) -> bool:
    if (index, pos) in exempt:  # 未消化的最新结果不动，哪怕内容重复
        return False
    if (index, pos) in superseded:
        return True
    text = _output_text(getattr(block, "output", ""))
    if len(text) < _MIN_TOOL_CHARS:
        return False
    return hashes.get((str(getattr(block, "name", "")), hash(text))) != index


def _dedupe_reads(
    messages: list[Msg], report: TrimReport, exempt: set[tuple[int, int]]
) -> list[Msg]:
    """同一份内容只留最后一次：旧的完全重复、或已被后来的区间覆盖的读取都省略。"""
    hashes = _last_by_hash(messages)
    superseded = _superseded_reads(messages, _call_inputs(messages))
    out = list(messages)
    for index, msg in enumerate(messages):
        if not isinstance(msg.content, list):
            continue
        blocks: list[Any] = []
        touched = False
        for pos, block in enumerate(msg.content):
            if not _is_tool_result(block) or not _should_drop(
                block, index, pos, hashes, superseded, exempt
            ):
                blocks.append(block)
                continue
            report.deduped_reads += 1
            report.reclaimed_chars += max(
                0, len(_output_text(getattr(block, "output", ""))) - len(_OMITTED)
            )
            blocks.append(block.model_copy(update={"output": _OMITTED}))
            touched = True
        if touched:
            out[index] = _with_blocks(msg, blocks)
    return out


def _turn_starts(messages: list[Msg]) -> list[int]:
    """用户输入消息的位置；系统注脚与框架摘要不算一轮的开始。"""
    return [index for index, msg in enumerate(messages) if is_user_turn(msg)]


def _drop_old_turns(messages: list[Msg], max_turns: int, report: TrimReport) -> list[Msg]:
    starts = _turn_starts(messages)
    if len(starts) <= max_turns:
        return messages
    keep_from = starts[len(starts) - max_turns]
    head = messages[: starts[0]]
    report.dropped_turns += len(starts) - max_turns
    return head + messages[keep_from:]


def _settle_stage(report: TrimReport) -> None:
    """记「最深生效的阶段」：事件里据此区分早退、去重、截断、淘汰。"""
    if report.stage == "s5":
        return
    if report.dropped_turns:
        report.stage = "s4"
    elif report.dropped_tool_results:
        report.stage = "s3"
    elif report.deduped_reads:
        report.stage = "s2"


def _open_report(messages: list[Msg], history_limit: int | None) -> TrimReport:
    """裁剪前的账面：预算上限与当前估算，供 s0 早退与最终对账共用。"""
    report = TrimReport()
    report.limit = int(history_limit or 0)
    report.tokens_before = estimate_messages(messages)
    return report


def _close_report(report: TrimReport, trimmed: list[Msg], history_limit: int | None) -> None:
    """裁剪后的账面：`over_budget` 只在真的给了预算时才判定（无预算＝不设限）。"""
    report.tokens_after = estimate_messages(trimmed)
    report.over_budget = history_limit is not None and report.tokens_after > history_limit


def trim_messages(
    messages: list[Msg],
    policy: ShortTermPolicy,
    history_limit: int | None = None,
) -> tuple[list[Msg], TrimReport]:
    """对请求 messages 做一次确定性裁剪；返回新列表与统计。

    `history_limit`（token）为空表示不做预算判定——只有测试与「关闭预算早退」
    的对照路径会这么调；生产路径必须传，否则退化成每轮乱砍。
    顺序：预算早退 → 去重 → 旧轮次截断 → 淘汰最旧轮次 → 仍超预算则压最新结果。
    """
    report = _open_report(messages, history_limit)
    if policy.trim_on_budget and history_limit is not None:
        if report.tokens_before <= history_limit:
            report.stage = "s0"
            report.tokens_after = report.tokens_before
            return list(messages), report
    trimmed = list(messages)
    exempt = _exempt_positions(trimmed, policy.recent_exempt_results)
    report.exempt_results = len(exempt)
    if policy.dedup_reads:
        # 去重必须看原文：先截断会让「同一份输出」变成同一段头尾，反而难以识别
        trimmed = _dedupe_reads(trimmed, report, exempt)
    trimmed = [
        _truncate_blocks(
            msg,
            index,
            policy,
            report,
            exempt,
            limit=policy.tool_output_chars,
            only_exempt=False,
        )
        for index, msg in enumerate(trimmed)
    ]
    trimmed = _drop_old_turns(trimmed, policy.max_turns, report)
    if history_limit is not None and estimate_messages(trimmed) > history_limit:
        # 丢整轮会改变下标，豁免位置必须重算；没有豁免块时无对象可压
        fresh = _exempt_positions(trimmed, policy.recent_exempt_results)
        if fresh:
            trimmed = [
                _truncate_blocks(
                    msg,
                    index,
                    policy,
                    report,
                    fresh,
                    limit=policy.recent_output_chars,
                    only_exempt=True,
                )
                for index, msg in enumerate(trimmed)
            ]
            report.stage = "s5"
    _settle_stage(report)
    _close_report(report, trimmed, history_limit)
    return trimmed, report


def _size(messages: list[Msg]) -> int:
    return len(json.dumps([msg.model_dump(mode="json") for msg in messages], default=str))


def trim_context_for_snapshot(
    messages: list[Msg], policy: ShortTermPolicy, max_chars: int
) -> tuple[list[Msg], TrimReport]:
    """快照前的体积保护：**只丢整轮**，不做内容截断。

    这里不再复用 `trim_messages`：快照会把结果写回 `state.context`（切档、重建
    会话都会触发），一旦把常规 1600 截断带进来，原文就永久消失，而报告里只留下
    「裁剪过」这一个信号，分不清是「请求期临时裁剪」还是「因体积丢历史」。
    """
    report = TrimReport(stage="snapshot")
    trimmed = list(messages)
    while len(_turn_starts(trimmed)) > 1 and _size(trimmed) > max_chars:
        starts = _turn_starts(trimmed)
        dropped = trimmed[starts[0] : starts[1]]
        report.dropped_turns += 1
        report.reclaimed_chars += _size(dropped)
        trimmed = trimmed[: starts[0]] + trimmed[starts[1] :]
    return trimmed, report


__all__ = [
    "TrimReport",
    "trim_context_for_snapshot",
    "trim_messages",
    "truncate_output",
]
