"""短期记忆裁剪测试（SPEC-0013 §4.1 / §5.3 第 4 步）。"""

from agentscope.message import Msg, SystemMsg, ToolResultBlock, UserMsg

from devops_agent.memory.short_term import (
    trim_context_for_snapshot,
    trim_messages,
    truncate_output,
)
from devops_agent.memory_policy import ShortTermPolicy

POLICY = ShortTermPolicy(max_turns=12, tool_output_chars=100, tool_output_head_ratio=0.7)


def _tool(name: str, output: str) -> Msg:
    return Msg(
        name="tool",
        role="assistant",
        content=[ToolResultBlock(id=f"id-{name}", name=name, output=output)],
    )


def test_truncate_output_keeps_head_tail_and_marks_omitted() -> None:
    text = "a" * 100 + "b" * 1000
    shortened = truncate_output(text, 100, 0.7)
    assert shortened.startswith("a" * 70)
    assert "已截断" in shortened
    assert shortened.endswith("b" * 30)


def test_trim_truncates_long_tool_result_and_keeps_current_input() -> None:
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="s1", content="当前问题"),
        _tool("Read", "x" * 500),
    ]
    trimmed, report = trim_messages(messages, POLICY)
    assert report.dropped_tool_results == 1
    assert report.reclaimed_chars == 500 - len(trimmed[-1].content[0].output)  # type: ignore[union-attr]
    assert trimmed[1].content[0].text == "当前问题"  # type: ignore[union-attr]


def test_trim_dedupes_repeated_reads() -> None:
    body = "y" * 400
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="s1", content="q1"),
        _tool("Read", body),
        _tool("Read", body),
    ]
    _, report = trim_messages(messages, POLICY)
    assert report.deduped_reads == 1


def test_trim_drops_oldest_turns_but_keeps_latest() -> None:
    messages = [SystemMsg(name="system", content="sys")]
    for index in range(4):
        messages.append(UserMsg(name="s1", content=f"q{index}"))
    trimmed, report = trim_messages(messages, ShortTermPolicy(max_turns=2))
    assert report.dropped_turns == 2
    texts = [msg.content[0].text for msg in trimmed if msg.role == "user"]  # type: ignore[union-attr]
    assert texts == ["q2", "q3"]


def test_snapshot_cap_drops_turns_until_under_limit() -> None:
    messages = [SystemMsg(name="system", content="sys")]
    for index in range(5):
        messages.append(UserMsg(name="s1", content=f"{index}" + "z" * 500))
    trimmed, report = trim_context_for_snapshot(
        messages, ShortTermPolicy(max_turns=12), max_chars=900
    )
    assert report.dropped_turns >= 1
    assert trimmed[-1].content[0].text.startswith("4")  # type: ignore[union-attr]
