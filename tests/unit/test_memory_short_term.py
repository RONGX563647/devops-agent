"""短期记忆裁剪测试（SPEC-0013 §4.1 / SPEC-0019 §3–§5）。"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from agentscope.message import Msg, SystemMsg, ToolCallBlock, ToolResultBlock, UserMsg

from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.short_term import (
    trim_context_for_snapshot,
    trim_messages,
    truncate_output,
)
from devops_agent.memory.tokens import estimate_messages
from devops_agent.memory_policy import MemoryContextPolicy, MemoryPolicy, ShortTermPolicy

# 用例里显式给豁免数：默认值将来调整不该让断言静默变成「另一个场景」
POLICY = ShortTermPolicy(
    max_turns=12,
    tool_output_chars=100,
    tool_output_head_ratio=0.7,
    recent_exempt_results=2,
)


def _tool(name: str, output: str, call_id: str = "") -> Msg:
    return Msg(
        name="tool",
        role="assistant",
        content=[ToolResultBlock(id=call_id or f"id-{name}", name=name, output=output)],
    )


def _call(name: str, payload: dict[str, object], call_id: str) -> Msg:
    # 框架的 `ToolCallBlock.input` 是 JSON 字符串，不是 dict
    return Msg(
        name="assistant",
        role="assistant",
        content=[ToolCallBlock(id=call_id, name=name, input=json.dumps(payload))],
    )


def _history(*tail: Msg) -> list[Msg]:
    return [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="q"), *tail]


def test_truncate_output_marks_original_line_range() -> None:
    text = "\n".join(f"line-{index:03d}" for index in range(200))
    shortened, lines = truncate_output(text, 200, 0.7)
    assert lines > 0
    assert "已截断" in shortened
    # 切点在行边界：标记前最后一行是完整行，不是半行
    head = shortened.split("\n…[")[0]
    assert head.splitlines()[-1] == "line-014"
    span = re.search(r"原第 (\d+)–(\d+) 行", shortened)
    assert span is not None and int(span.group(1)) > 1


def test_recent_tool_results_are_exempt_from_truncation() -> None:
    messages = _history(*[_tool("Read", f"{index}" * 500) for index in range(4)])
    trimmed, report = trim_messages(messages, POLICY)
    assert report.exempt_results == 2
    assert report.dropped_tool_results == 2
    # 旧的两条被截（带可复原标记），最新的两条逐字节保留
    assert "已截断" in trimmed[2].content[0].output  # type: ignore[union-attr,index]
    assert trimmed[-1].content[0].output == "3" * 500  # type: ignore[union-attr,index]
    assert trimmed[-2].content[0].output == "2" * 500  # type: ignore[union-attr,index]
    # 当前用户输入永不裁
    assert trimmed[1].content[0].text == "q"  # type: ignore[union-attr,index]


def test_untrimmed_when_under_budget() -> None:
    messages = _history(_tool("Read", "x" * 500))
    trimmed, report = trim_messages(messages, POLICY, history_limit=100_000)
    assert report.changed is False
    assert report.stage == "s0"
    assert trimmed[2].content[0].output == "x" * 500  # type: ignore[union-attr,index]
    # 早退只保证元素不重建；列表本身必须是新的，否则记忆注脚会被 append 进历史
    assert trimmed is not messages
    assert trimmed[2] is messages[2]


def test_over_budget_truncates_only_old_results() -> None:
    messages = _history(*[_tool("Read", f"{index}" * 500) for index in range(3)])
    # 预算取 600：低于未裁剪的 697、高于只砍旧结果后的 563，才刚好落在 S3
    trimmed, report = trim_messages(messages, POLICY, history_limit=600)
    assert report.stage == "s3"
    assert "已截断" in trimmed[2].content[0].output  # type: ignore[union-attr,index]
    assert trimmed[-1].content[0].output == "2" * 500  # type: ignore[union-attr,index]


def test_single_turn_over_budget_falls_back_to_recent_cap() -> None:
    policy = ShortTermPolicy(
        tool_output_chars=100, recent_output_chars=200, recent_exempt_results=2
    )
    messages = _history(_tool("Read", "x" * 2000), _tool("Read", "y" * 2000))
    trimmed, report = trim_messages(messages, policy, history_limit=50)
    assert report.stage == "s5"
    latest = trimmed[-1].content[0].output  # type: ignore[union-attr,index]
    assert "已截断" in latest
    assert 200 < len(latest) < 400


def test_dedupe_by_read_span_supersedes_covered_read() -> None:
    span = {"file_path": "a.py", "offset": 1, "limit": 50}
    wider = {"file_path": "a.py", "offset": 1, "limit": 200}
    messages = _history(
        _call("Read", span, "c1"),
        _tool("Read", "y" * 400, "c1"),
        _call("Read", span, "c2"),
        _tool("Read", "z" * 400, "c2"),
        _call("Read", wider, "c3"),
        _tool("Read", "w" * 400, "c3"),
    )
    _, report = trim_messages(messages, POLICY)
    # c1 被 c3 的区间完全覆盖；c2 虽被覆盖但在豁免集里，不动
    assert report.deduped_reads == 1


def test_trim_dedupes_repeated_reads() -> None:
    body = "y" * 400
    messages = _history(_tool("Read", body), _tool("Read", body), _tool("Read", body))
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


def test_snapshot_trim_never_truncates_content() -> None:
    messages = _history(_tool("Read", "x" * 5000))
    trimmed, report = trim_context_for_snapshot(messages, POLICY, max_chars=10_000_000)
    assert report.dropped_turns == 0
    assert report.changed is False
    assert trimmed[2].content[0].output == "x" * 5000  # type: ignore[union-attr,index]


def test_cap_snapshot_does_not_bake_request_trim(tmp_path) -> None:  # type: ignore[no-untyped-def]
    runtime = MemoryRuntime(session_id="s1", policy=MemoryPolicy(), db_path=tmp_path / "obs.db")
    context = _history(_tool("Read", "x" * 5000))
    agent = SimpleNamespace(state=SimpleNamespace(context=context))
    report = runtime.cap_snapshot(agent)  # type: ignore[arg-type]
    assert report.dropped_turns == 0
    # 请求期的 1600 截断绝不能借快照写回历史（切档/重建都会触发快照）
    assert agent.state.context[2].content[0].output == "x" * 5000  # type: ignore[index,union-attr]


def test_estimate_includes_tool_output() -> None:
    """工具结果必须计入估算（旧实现按 `.text` 取值等于算 0，预算早退永远成立）。

    阈值按 qwen 族系数（拉丁字母约 5.57 字/token）取 800，留出族切换的余量；
    钉死的不是具体数值，而是「5000 字输出不能被当成几十 token」。
    """
    assert estimate_messages([_tool("Read", "x" * 5000)]) > 800


def test_zero_exempt_and_off_budget_matches_old_behaviour() -> None:
    policy = ShortTermPolicy(tool_output_chars=100, recent_exempt_results=0, trim_on_budget=False)
    trimmed, report = trim_messages(
        _history(_tool("Read", "x" * 500)), policy, history_limit=10_000_000
    )
    assert report.dropped_tool_results == 1
    assert "已截断" in trimmed[-1].content[0].output  # type: ignore[union-attr,index]


def test_cap_snapshot_stamps_version_and_reserves_summary_room(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """版本号要写进 middle_context，且历史额度扣掉摘要占用的字符。

    `stamp_snapshot` 此前没有调用方，`/api/context` 永远显示 1；摘要与历史
    各按 `snapshot_max_chars` 上限算，合计必然超限。
    """
    policy = MemoryPolicy(context=MemoryContextPolicy(snapshot_max_chars=10_000))
    runtime = MemoryRuntime(session_id="s1", policy=policy, db_path=tmp_path / "obs.db")
    # 三个轮次：裁「整轮」需要一个以上的轮次边界，单轮历史再怎么超都只能就地截断
    history = _history(
        UserMsg(name="u1", content="a"),
        _tool("Read", "x" * 800),
        UserMsg(name="u2", content="b"),
        _tool("Read", "y" * 800),
        UserMsg(name="u3", content="c"),
        _tool("Read", "z" * 800),
    )

    # 对照：没有摘要时历史不超上限，快照不该动它
    control = SimpleNamespace(state=SimpleNamespace(context=list(history), middle_context={}))
    runtime.cap_snapshot(control)  # type: ignore[arg-type]
    assert control.state.middle_context["snapshot_version"] == policy.context.snapshot_version
    assert len(control.state.context) == len(history)

    # 摘要吃掉 9600 字后只剩 400 字额度，必须继续丢最旧轮次
    loaded = SimpleNamespace(
        state=SimpleNamespace(
            context=list(history),
            middle_context={},
            summary=[SimpleNamespace(text="摘" * 9600)],
        )
    )
    report = runtime.cap_snapshot(loaded)  # type: ignore[arg-type]
    assert report.dropped_turns > 0
    assert len(loaded.state.context) < len(history)
