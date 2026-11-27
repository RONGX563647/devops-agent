"""前缀缓存契约（SPEC-0016 §6 / §10 A27–A28）。

缓存失效不是「感觉变慢了」，而应该能被指认：这一轮的前缀指纹变没变、历史从第
几条开始变、是哪个阶段改的。这一组把 V1–V5 钉成可执行的断言。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from agentscope.agent import ContextConfig
from agentscope.message import Msg, SystemMsg, ToolResultBlock, UserMsg

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.observer import MemoryMiddleware, prefix_fingerprint
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink

TOOLS = [
    {"name": "Bash", "description": "run", "parameters": {"type": "object"}},
    {"name": "Read", "description": "read", "parameters": {"type": "object"}},
]


class _FakeModel:
    def __init__(self, context_size: int = 128000) -> None:
        self.context_size = context_size

    async def count_tokens(self, **_kwargs: Any) -> int:
        return 100


class _FakeState:
    def __init__(self) -> None:
        self.summary = ""
        self.context: list[Any] = []
        self.middle_context: dict[str, Any] = {}
        self.reply_id = "r1"


class _FakeAgent:
    def __init__(self, context_size: int = 128000) -> None:
        self.model = _FakeModel(context_size)
        self.state = _FakeState()
        self.context_config = ContextConfig()

    async def _prepare_model_input(self) -> dict[str, Any]:
        return {"messages": []}


def _runtime(tmp_path: Any) -> MemoryRuntime:
    return MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")


def _call(
    middleware: MemoryMiddleware,
    agent: _FakeAgent,
    messages: list[Msg],
    tools: list[dict[str, Any]] | None = None,
) -> list[Msg]:
    captured: dict[str, list[Msg]] = {}

    async def handler(**kwargs: Any) -> Any:
        captured["messages"] = kwargs["messages"]
        return SimpleNamespace(usage=None)

    asyncio.run(
        middleware.on_model_call(
            agent, {"messages": messages, "tools": TOOLS if tools is None else tools}, handler
        )
    )
    return captured["messages"]


def _assembled(frames: list[dict[str, Any]]) -> dict[str, Any]:
    items = [frame for frame in frames if frame["type"] == events.CONTEXT_ASSEMBLED]
    assert len(items) == 1
    return dict(items[0]["memory"])


def _tool(name: str, output: str) -> Msg:
    return Msg(
        name="tool",
        role="assistant",
        content=[ToolResultBlock(id=f"id-{name}", name=name, output=output)],
    )


def test_fingerprint_is_stable_under_tool_order() -> None:
    """V1：工具按固定名序参与指纹，dict 顺序变化不该让缓存全冷。"""
    assert prefix_fingerprint("sys", TOOLS) == prefix_fingerprint("sys", list(reversed(TOOLS)))
    assert prefix_fingerprint("sys", TOOLS) != prefix_fingerprint("sys2", TOOLS)
    assert prefix_fingerprint("sys", TOOLS) != prefix_fingerprint("sys", TOOLS[1:])


def test_unchanged_turn_reports_no_history_change(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """V3 + A28：未超预算的两轮，历史逐字节不变，指纹复用。"""
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = _FakeAgent()
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]

    _call(middleware, agent, messages)
    first = _assembled(runtime.sink.drain())
    agent.state.reply_id = "r2"
    _call(middleware, agent, messages)
    second = _assembled(runtime.sink.drain())

    assert first["cache"]["prefix_reused"] is False  # 冷启动
    assert second["cache"]["prefix_reused"] is True
    assert second["cache"]["history_changed_at"] == -1
    assert second["cache"]["history_changed_by"] == "none"
    assert second["cache"]["cached_prefix_tokens"] > 0


def test_new_message_shifts_change_point(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """追加一条消息：缓存从新消息的位置开始失效，前面仍然可复用。"""
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = _FakeAgent()
    base = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]
    _call(middleware, agent, base)
    runtime.sink.drain()

    agent.state.reply_id = "r2"
    grown = base + [UserMsg(name="s2", content="再加一句")]
    _call(middleware, agent, grown)
    cache = _assembled(runtime.sink.drain())["cache"]
    assert cache["history_changed_at"] == len(base)
    assert cache["history_changed_by"] == "none"


def test_system_prompt_change_restarts_cache(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """V1：系统提示变了，前缀指纹必须变，且失效点是第 0 条。"""
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = _FakeAgent()
    _call(
        middleware,
        agent,
        [SystemMsg(name="system", content="sys"), UserMsg(name="u", content="q")],
    )
    runtime.sink.drain()

    agent.state.reply_id = "r2"
    _call(
        middleware,
        agent,
        [SystemMsg(name="system", content="sys-v2"), UserMsg(name="u", content="q")],
    )
    cache = _assembled(runtime.sink.drain())["cache"]
    assert cache["prefix_reused"] is False
    assert cache["history_changed_at"] == 0


def test_note_is_appended_at_the_tail_only(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """V2：注入只能追加在末尾，不得插进历史中段。"""
    runtime = _runtime(tmp_path)
    runtime.episodic.save(
        "s1", text="目标：修好登录", trigger="t", tokens_before=10, tokens_after=5, budget=8
    )
    middleware = MemoryMiddleware(runtime)
    agent = _FakeAgent()
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]
    sent = _call(middleware, agent, messages)

    assert len(sent) == len(messages) + 1
    assert sent[: len(messages)] == messages
    assert getattr(sent[-1], "name", "") == "system"
    assert "会话记忆" in str(getattr(sent[-1], "content", ""))


def test_note_is_reused_within_one_turn(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """V5：同一轮内多次模型调用复用同一份 note，装配视图也只发一次。"""
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = _FakeAgent()
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]
    first = _call(middleware, agent, messages)
    frames = runtime.sink.drain()
    second = _call(middleware, agent, messages)
    assert runtime.sink.drain() == []
    assert [str(item.content) for item in first] == [str(item.content) for item in second]
    assert _assembled(frames)["cache"]["estimated"] is True


def test_over_budget_attributes_the_change(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """A28 后半：超预算时改动必须能归因到具体阶段。

    窗口调小到 20k，让 40 轮 × 4k 字符的工具输出必然越过 `history_limit`；
    先跑一轮小的建立「上一轮」基线，再跑大的，才能看出失效点与归因。
    """
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = _FakeAgent(context_size=20000)
    _call(
        middleware,
        agent,
        [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="基线")],
    )
    runtime.sink.drain()

    agent.state.reply_id = "r2"
    messages: list[Msg] = [SystemMsg(name="system", content="sys")]
    for index in range(40):
        messages.append(UserMsg(name="u", content=f"q{index}"))
        messages.append(_tool(f"t{index}", f"{index}" * 2000))
    _call(middleware, agent, messages)
    cache = _assembled(runtime.sink.drain())["cache"]
    assert cache["history_changed_by"] in {"trim_tail", "dedup", "trim_mid", "drop_turns"}
    assert cache["history_changed_at"] >= 0
