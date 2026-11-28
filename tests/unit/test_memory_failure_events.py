"""记忆失败必须可见（SPEC-0015 §4）。

三条断言对应三种「静默」：召回被吞成空结果、回合末写入裸 pass、以及回合末
产生的帧在流结束后没人 drain。它们合起来的效果是记忆悄悄停摆，而所有接口
都显示正常。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agentscope.agent import ContextConfig
from agentscope.message import SystemMsg, UserMsg

from devops_agent.config import load_memory
from devops_agent.memory import events, health
from devops_agent.memory.observer import MemoryMiddleware
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.runtime.scope_runtime import AgentScopeRuntime


class FakeModel:
    context_size = 128000

    async def count_tokens(self, **_kwargs: Any) -> int:
        return 100


class FakeState:
    def __init__(self) -> None:
        self.summary = ""
        self.context: list[Any] = []
        self.middle_context: dict[str, Any] = {}
        self.reply_id = "r1"


class FakeAgent:
    def __init__(self) -> None:
        self.model = FakeModel()
        self.state = FakeState()
        self.context_config = ContextConfig()


class BrokenBackend:
    """模拟后端不可用：读与写都抛。"""

    reason = "broken"

    async def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("backend down")

    async def write(self, _text: str) -> dict[str, Any]:
        raise RuntimeError("backend down")


@pytest.fixture(autouse=True)
def _clean_health() -> None:
    health.reset()


def _runtime(tmp_path: Any) -> MemoryRuntime:
    return MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")


def _events(runtime: MemoryRuntime, kind: str) -> list[dict[str, Any]]:
    return [frame for frame in runtime.sink.drain() if frame["type"] == kind]


def test_recall_reports_degraded_layers(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    runtime.long_term = BrokenBackend()
    result = asyncio.run(runtime.recall("q"))
    assert result.hits == []
    assert result.degraded == ("long_term",)
    assert result.reason == events.REASON_LONG_TERM_UNAVAILABLE
    assert health.snapshot()["degraded"] is True


def test_retrieve_end_is_error_not_miss(tmp_path: Any) -> None:
    """后端挂了不能显示成 `miss/no_memory` —— 那正是故障被伪装的地方。"""
    runtime = _runtime(tmp_path)
    runtime.long_term = BrokenBackend()
    middleware = MemoryMiddleware(runtime)
    agent = FakeAgent()
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]

    async def next_handler(**_kwargs: Any) -> str:
        return "ok"

    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    end = _events(runtime, events.MEMORY_RETRIEVE_END)[0]
    assert end["state"] == "error"
    assert end["reason"] == events.REASON_LONG_TERM_UNAVAILABLE
    assert end["memory"]["degraded"] == ["long_term"]


def test_turn_write_failure_emits_event(tmp_path: Any) -> None:
    """回合末写入的兜底 except 不许是裸 pass。"""
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = FakeAgent()
    agent.state.context = []

    async def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("disk full")

    middleware._runtime.write_turn = boom  # type: ignore[method-assign]  # 模拟外层异常

    async def source():  # type: ignore[no-untyped-def]
        yield {"type": "REPLY_END"}

    async def collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in middleware.on_reply(
                agent,
                {"inputs": UserMsg(name="s1", content="hi")},
                source,  # type: ignore[arg-type]
            )
        ]

    assert asyncio.run(collect()) == [{"type": "REPLY_END"}]
    end = _events(runtime, events.MEMORY_WRITE_END)[0]
    assert end["layer"] == "turn"
    assert end["state"] == "error"
    assert end["reason"] == events.REASON_TURN_WRITE_FAILED
    assert end["memory"]["exc_type"] == "RuntimeError"
    assert health.snapshot()["degraded"] is True


def test_status_exposes_health(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    runtime.long_term = BrokenBackend()
    asyncio.run(runtime.recall("q"))
    status = runtime.status()
    assert status["health"]["degraded"] is True
    assert status["health"]["layers"]["long_term"]["failures"] == 1


def test_stream_flushes_frames_produced_after_last_event(tmp_path: Any) -> None:
    """`on_reply` 的 finally 在循环取下一个元素时才执行完，尾帧要补发。"""
    runtime = _runtime(tmp_path)

    class Streaming:
        def __init__(self) -> None:
            self.state = FakeState()

        async def reply_stream(self, _msg: object):  # type: ignore[no-untyped-def]
            yield type("E", (), {"model_dump": lambda self, mode="json": {"type": "REPLY_END"}})()
            # 模拟中间件 finally：帧在最后一个事件之后才进队列
            runtime.sink.emit(events.frame(events.MEMORY_WRITE_END, layer="turn"))

    async def collect() -> list[dict[str, Any]]:
        scope = AgentScopeRuntime(Streaming(), memory=runtime)  # type: ignore[arg-type]
        return [frame async for frame in scope.stream("s1", "q")]

    frames = asyncio.run(collect())
    assert frames[-1]["type"] == events.MEMORY_WRITE_END
