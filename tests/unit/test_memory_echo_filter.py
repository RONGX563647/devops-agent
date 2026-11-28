"""召回去重：已经在本轮上下文里的命中不再注入。

实测症状是「召回块里是我上一轮的开场白」—— 长期层存的是「用户原话 + 结论」，
而上一轮这段本来就在历史里，原样注入等于让模型复读自己，还占掉 note 预算。
历史被预算裁掉之后同一条必须重新生效：那正是长期层存在的理由。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agentscope.message import (
    Msg,
    SystemMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.long_term import Hit
from devops_agent.memory.observer import MemoryMiddleware, _context_blob, _drop_echoes
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink

#: 召回条目：与下面用户消息有足够的词面重合，能过 min_score
ENTRY = "记忆写入的正文只保留结论，不保留工具调用的参数 JSON，因此召回不会把自己说过的话还回来。"
QUESTION = "记忆为什么总是召回自己说过的话？"
ANSWER = "结论：长期层写入前要先剔除工具入参，因此摘要只会保留结论句。"


def _runtime(tmp_path: Path) -> MemoryRuntime:
    return MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")


def _agent() -> Any:
    class _Model:
        context_size = 128_000

    class _State:
        context: list[Any] = []
        reply_id = "r1"

    class _Agent:
        model = _Model()
        state = _State()
        context_config = None

    return _Agent()


def _assemble(runtime: MemoryRuntime, messages: list[Any]) -> tuple[str, dict[str, Any]]:
    middleware = MemoryMiddleware(runtime)
    budget = runtime.budget(None, 0)
    return asyncio.run(middleware._assemble(_agent(), messages, "t1", budget))


def _seed(runtime: MemoryRuntime) -> None:
    asyncio.run(runtime.long_term.write(ENTRY, kind="fact"))


def test_hit_already_in_history_is_not_injected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _seed(runtime)
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="s1", content="记忆写入的正文应该保留结论吗？"),
        Msg(
            name="devops",
            role="assistant",
            content=[TextBlock(type="text", text=ENTRY)],
        ),
    ]
    note, meta = _assemble(runtime, messages)
    assert meta["echo_dropped"] == 1
    assert meta["hits"] == 0
    assert note == ""
    end = [f for f in runtime.sink.drain() if f["type"] == events.MEMORY_RETRIEVE_END]
    assert end and end[0]["reason"] == events.REASON_ALREADY_IN_CONTEXT


def test_hit_is_injected_again_after_history_is_trimmed(tmp_path: Path) -> None:
    """历史被裁掉之后同一条命中要重新生效 —— 去重不能变成「永久不注入」。"""
    runtime = _runtime(tmp_path)
    _seed(runtime)
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="s1", content="记忆写入的正文应该保留结论吗？"),
    ]
    note, meta = _assemble(runtime, messages)
    assert meta["echo_dropped"] == 0
    assert meta["hits"] == 1
    assert "结论" in note


def test_short_hits_are_never_dropped_by_substring() -> None:
    """短串到处都能子串命中：按它过滤会把真正有用的短结论一起丢掉。"""
    short = Hit(id=1, text="用 ruff", score=0.9)
    long_hit = Hit(id=2, text=ENTRY, score=0.9)
    kept = _drop_echoes([short, long_hit], "以后统一用ruff不要用flake8" + ENTRY.replace("，", ""))
    assert short in kept
    assert long_hit not in kept


def test_tool_messages_between_question_and_answer_do_not_defeat_the_filter() -> None:
    """问题与结论之间隔着工具调用时，整串判定会失效 —— 必须逐句判。

    这是最有价值的回合形态（带工具的 ReAct 轮次）：用户问题、工具调用、工具结果、
    最终结论是四条消息，拼接串里问题与结论并不相邻。旧实现拿整条记忆做子串判定，
    恰好在这类回合漏掉「复读自己」。
    """
    entry = f"{QUESTION}\n{ANSWER}"
    messages = [
        SystemMsg(name="system", content="sys"),
        UserMsg(name="s1", content=QUESTION),
        Msg(
            name="devops",
            role="assistant",
            content=[ToolCallBlock(type="tool_call", id="c1", name="Grep", input='{"p": 1}')],
        ),
        Msg(
            name="devops",
            role="assistant",
            content=[ToolResultBlock(type="tool_result", id="c1", name="Grep", output="a.py:1")],
        ),
        Msg(name="devops", role="assistant", content=[TextBlock(type="text", text=ANSWER)]),
    ]
    assert _drop_echoes([Hit(id=1, text=entry, score=1.0)], _context_blob(messages)) == []


def test_hit_with_one_new_sentence_is_still_injected() -> None:
    """只有一部分已在历史里：仍然注入 —— 新句子就是要补的信息。"""
    partial = f"{ANSWER}\n另外：检索用的是 BM25，min_score 默认 0.30。"
    kept = _drop_echoes([Hit(id=1, text=partial, score=0.9)], ANSWER)
    assert len(kept) == 1
