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


def test_untouched_history_keeps_every_element() -> None:
    """A26 / V3：未超预算（s0 早退）不得重建任何消息，只换外壳列表。

    外壳必须是新列表：调用方随后会往尾部 append 记忆注脚，共享历史会被污染。
    """
    messages = _history(2, size=400)
    trimmed, report = trim_messages(messages, POLICY, history_limit=100_000)
    assert trimmed is not messages
    assert all(a is b for a, b in zip(trimmed, messages, strict=True))
    assert report.changed is False
    assert report.stage == "s0"
    assert report.tokens_before == report.tokens_after


def test_over_budget_history_is_trimmed_under_the_limit() -> None:
    """A25：20 轮 × 3k 字工具结果 → 裁剪后必须装进 history_limit。"""
    messages = _history(20)
    limit = 8000
    trimmed, report = trim_messages(messages, POLICY, history_limit=limit)
    assert count_messages(trimmed) <= limit
    assert report.over_budget is False
    assert report.stage in {"s3", "s4", "s5"}
    # 当前用户输入永不截断
    assert trimmed[-2].content[0].text.startswith("第 19 轮")  # type: ignore[union-attr]


def test_single_oversized_result_lands_in_s5() -> None:
    """只有一条超大结果可砍时最深只到 s5（收紧当前轮），且用户输入不受影响。"""
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="u", content="问题")]
    messages.append(_tool("big", "x" * 60_000))
    trimmed, report = trim_messages(messages, POLICY, history_limit=5000)
    assert report.stage == "s5"
    assert report.changed_by == "trim_tail"
    assert trimmed[1].content[0].text == "问题"  # type: ignore[union-attr]


def test_dedup_only_under_pressure() -> None:
    """A28：未超预算时历史中段不得被改写（预算早退先于去重）。"""
    body = "y" * 500
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="u1", content="q1"),
        _tool("Read", body),
        UserMsg(name="u2", content="q2"),
        _tool("Read", body),
    ]
    kept, report = trim_messages(messages, POLICY, history_limit=100_000)
    assert all(a is b for a, b in zip(kept, messages, strict=True))
    assert report.deduped_reads == 0

    squeezed, squeezed_report = trim_messages(messages, POLICY, history_limit=10)
    # 预算极小时必然报超预算；同一条内容没有读区间信息，去重不会误伤它
    assert squeezed_report.over_budget is True
    assert squeezed_report.deduped_reads == 0


def test_dedup_disabled_keeps_repeated_reads() -> None:
    """`dedup_reads=False` 时同一份内容重复出现也不改写。"""
    body = "y" * 500
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="u1", content="q1"),
        _tool("Read", body),
        UserMsg(name="u2", content="q2"),
        _tool("Read", body),
    ]
    _, report = trim_messages(messages, ShortTermPolicy(dedup_reads=False), history_limit=10)
    assert report.deduped_reads == 0


def test_single_huge_turn_reports_over_budget() -> None:
    """一轮本身就装不下：s5 收紧当前轮，仍超则如实报 over_budget。"""
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="u", content="问题")]
    messages.append(_tool("huge", "z" * 200_000))
    # 预算小到「收紧当前轮也不够」：s5 之后仍超，才算 over_budget
    trimmed, report = trim_messages(messages, POLICY, history_limit=10)
    assert report.stage == "s5"
    assert report.over_budget is True
    assert trimmed[1].content[0].text == "问题"  # type: ignore[union-attr]


def test_report_carries_budget_fields_for_events() -> None:
    """A34 前置：MEMORY_TRIM 需要 stage/limit/over_budget，字段必须进 as_dict。"""
    _, report = trim_messages(_history(20), POLICY, history_limit=4000)
    payload: dict[str, Any] = report.as_dict()
    assert {"stage", "limit", "over_budget", "tokens_before", "tokens_after"} <= set(payload)
    assert payload["limit"] == 4000
