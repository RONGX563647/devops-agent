"""预算执行与五阶段裁剪（SPEC-0016 §5 / §10 A25–A28）。

这一组盯的是「预算是硬约束」：超预算时按固定顺序改，未超预算时一个字节都不动。
顺序不是审美问题 —— 越靠前的阶段改动点越靠后，废掉的缓存越短。
"""

from __future__ import annotations

from typing import Any

from agentscope.message import Msg, SystemMsg, ToolResultBlock, UserMsg

from devops_agent.memory.budget import compute_budget
from devops_agent.memory.short_term import trim_messages
from devops_agent.memory.tokens import count_messages
from devops_agent.memory_policy import ShortTermPolicy

POLICY = ShortTermPolicy(tool_output_chars=1600, tool_output_head_ratio=0.7)


def _tool(name: str, output: str) -> Msg:
    return Msg(
        name="tool",
        role="assistant",
        content=[ToolResultBlock(id=f"id-{name}", name=name, output=output)],
    )


def _turn(index: int, size: int = 3000) -> list[Msg]:
    return [UserMsg(name="u", content=f"第 {index} 轮的问题"), _tool(f"t{index}", "x" * size)]


def _history(turns: int, size: int = 3000) -> list[Msg]:
    messages: list[Msg] = [SystemMsg(name="system", content="sys")]
    for index in range(turns):
        messages.extend(_turn(index, size))
    return messages


def test_budget_closes_within_rounding() -> None:
    """A25 前置：四项分账之和必须闭合到阈值，差额只来自取整。"""
    budget = compute_budget(
        context_size=128000,
        trigger_ratio=0.8,
        reserve_ratio=0.1,
        max_tokens=None,
        prefix_tokens=3060,
        short_term_ratio=0.5,
        episodic_ratio=0.3,
        long_term_ratio=0.1,
        headroom_ratio=0.1,
    )
    assert budget.threshold == 102400
    assert budget.output_reserve == 12800
    assert budget.history_limit + budget.note_limit + budget.fixed + budget.output_reserve <= (
        budget.threshold + 4
    )
    assert budget.ok is True


def test_untouched_history_is_returned_as_the_same_object() -> None:
    """A26 / V3：未超预算必须返回同一个 list 对象，否则等于每轮重建历史。"""
    messages = _history(2, size=400)
    trimmed, report = trim_messages(messages, POLICY, max_tokens=100_000)
    assert trimmed is messages
    assert report.changed is False
    assert report.stage == ""
    assert report.tokens_before == report.tokens_after


def test_over_budget_history_is_trimmed_under_the_limit() -> None:
    """A25：20 轮 × 3k 字工具结果 → 裁剪后必须装进 history_limit。"""
    messages = _history(20)
    limit = 4000
    trimmed, report = trim_messages(messages, POLICY, max_tokens=limit)
    assert count_messages(trimmed) <= limit
    assert report.over_budget is False
    assert report.stage in {"S1", "S3", "S4", "S5"}
    # 当前用户输入永不截断
    assert trimmed[-2].content[0].text.startswith("第 19 轮")  # type: ignore[union-attr]


def test_tail_overflow_lands_in_s1() -> None:
    """只超在尾巴上时实测点应当最靠后：S1 只动最后一条 user 之后。"""
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="u", content="问题")]
    messages.append(_tool("big", "x" * 60_000))
    trimmed, report = trim_messages(messages, POLICY, max_tokens=5000)
    assert report.stage == "S1"
    assert report.changed_by == "trim_tail"
    assert trimmed[1].content[0].text == "问题"  # type: ignore[union-attr]


def test_dedup_only_under_pressure() -> None:
    """A28：默认 on_pressure —— 未超预算时历史中段不得被改写。"""
    body = "y" * 500
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="u1", content="q1"),
        _tool("Read", body),
        UserMsg(name="u2", content="q2"),
        _tool("Read", body),
    ]
    kept, report = trim_messages(messages, POLICY, max_tokens=100_000)
    assert kept is messages
    assert report.deduped_reads == 0

    squeezed, squeezed_report = trim_messages(messages, POLICY, max_tokens=10)
    # 极限预算下会一路走到 S5，但「去重发生了」这一点必须成立
    assert squeezed_report.deduped_reads == 1
    assert squeezed_report.stage in {"S2", "S3", "S4", "S5"}


def test_never_mode_skips_dedup_even_under_pressure() -> None:
    body = "y" * 500
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="u1", content="q1"),
        _tool("Read", body),
        UserMsg(name="u2", content="q2"),
        _tool("Read", body),
    ]
    _, report = trim_messages(
        messages, ShortTermPolicy(dedup_reads="never"), max_tokens=10
    )
    assert report.deduped_reads == 0


def test_single_huge_turn_reports_over_budget() -> None:
    """一轮本身就装不下：S5 收紧当前轮，仍超则如实报 over_budget。"""
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="u", content="问题")]
    messages.append(_tool("huge", "z" * 200_000))
    # 预算小到「收紧当前轮也不够」：S5 之后仍超，才算 over_budget
    trimmed, report = trim_messages(messages, POLICY, max_tokens=10)
    assert report.stage == "S5"
    assert report.over_budget is True
    assert trimmed[1].content[0].text == "问题"  # type: ignore[union-attr]


def test_report_carries_budget_fields_for_events() -> None:
    """A34 前置：MEMORY_TRIM 需要 stage/limit/over_budget，字段必须进 as_dict。"""
    _, report = trim_messages(_history(20), POLICY, max_tokens=4000)
    payload: dict[str, Any] = report.as_dict()
    assert {"stage", "limit", "over_budget", "tokens_before", "tokens_after"} <= set(payload)
    assert payload["limit"] == 4000
