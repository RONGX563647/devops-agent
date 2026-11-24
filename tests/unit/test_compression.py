"""上下文压缩可观测性测试：钩子采集、帧顺序、装配。

这些用例不碰真实模型：token 计数与上下文都由假对象提供，
要验证的是「压缩发生时前端能看到什么」。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from agentscope.agent import Agent, ContextConfig

from devops_agent.runtime.assembly import build_agentscope_runtime
from devops_agent.runtime.compression import (
    CONTEXT_BUDGET,
    CONTEXT_COMPRESSION_END,
    CONTEXT_COMPRESSION_START,
    CompressionMiddleware,
    CompressionSink,
    context_budget,
    count_context_tokens,
)
from devops_agent.runtime.scope_runtime import AgentScopeRuntime


class FakeModel:
    """只提供压缩链路需要的两个属性：窗口大小与计数能力。"""

    context_size = 128000

    def __init__(self, tokens: int = 0, fail: bool = False) -> None:
        self.tokens = tokens
        self.fail = fail

    async def count_tokens(self, **_kwargs: Any) -> int:
        if self.fail:
            raise RuntimeError("计数失败")
        return self.tokens


class FakeState:
    def __init__(self, summary: str = "", context: list[Any] | None = None) -> None:
        self.summary = summary
        self.context = context if context is not None else []


class FakeAgent:
    def __init__(
        self,
        tokens: int = 0,
        context_size: int = 128000,
        summary: str = "",
        context: list[Any] | None = None,
        fail_count: bool = False,
    ) -> None:
        self.model = FakeModel(tokens=tokens, fail=fail_count)
        self.model.context_size = context_size
        self.state = FakeState(summary=summary, context=context)
        self.context_config = ContextConfig()

    async def _prepare_model_input(self) -> dict[str, Any]:
        return {"messages": []}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_context_budget_uses_trigger_ratio_and_window() -> None:
    """阈值必须与框架口径一致：0.8 × 128000 = 102400。"""
    frame = context_budget(FakeAgent())  # type: ignore[arg-type]
    assert frame == {
        "type": CONTEXT_BUDGET,
        "threshold": 102400,
        "context_size": 128000,
        "trigger_ratio": 0.8,
    }


def test_context_budget_survives_missing_model_size() -> None:
    """模型没有 context_size 时退回默认窗口，不抛异常。"""

    class Bare:
        context_config = ContextConfig()

    frame = context_budget(Bare())  # type: ignore[arg-type]
    assert frame["context_size"] == 128000
    assert frame["threshold"] == 102400


def test_count_tokens_returns_zero_when_counting_fails() -> None:
    """计数失败只影响展示，绝不能让压缩主流程炸掉。"""
    agent = FakeAgent(fail_count=True)
    assert _run(count_context_tokens(agent)) == 0  # type: ignore[arg-type]


def test_middleware_reports_compressed_when_summary_changes() -> None:
    """真压缩：START 带检查前 token，END 带压缩后 token 与摘要长度。"""
    agent = FakeAgent(tokens=103053)
    sink = CompressionSink()
    middleware = CompressionMiddleware(sink)

    async def next_handler(**_kwargs: Any) -> None:
        # 模拟框架把上下文压成摘要
        agent.state.summary = "摘要" * 100
        agent.model.tokens = 21000

    _run(
        middleware.on_compress_context(
            agent=agent,  # type: ignore[arg-type]
            input_kwargs={},
            next_handler=next_handler,
        )
    )

    frames = sink.drain()
    assert [frame["type"] for frame in frames] == [
        CONTEXT_COMPRESSION_START,
        CONTEXT_COMPRESSION_END,
    ]
    assert frames[0]["before_tokens"] == 103053
    assert frames[0]["threshold"] == 102400
    assert frames[1]["after_tokens"] == 21000
    assert frames[1]["compressed"] is True
    assert frames[1]["summary_chars"] == 200


def test_middleware_marks_noop_when_below_threshold() -> None:
    """没到阈值：框架直接 return，END 里 compressed 必须是 False。

    前端据此只画真正压缩的记录，不然每轮推理都会刷一条空记录。
    """
    agent = FakeAgent(tokens=5000, summary="旧摘要")
    sink = CompressionSink()
    middleware = CompressionMiddleware(sink)

    async def next_handler(**_kwargs: Any) -> None:
        return None

    _run(
        middleware.on_compress_context(
            agent=agent,  # type: ignore[arg-type]
            input_kwargs={},
            next_handler=next_handler,
        )
    )

    end = sink.drain()[-1]
    assert end["type"] == CONTEXT_COMPRESSION_END
    assert end["compressed"] is False


def test_stream_emits_budget_then_drains_compression_frames() -> None:
    """流顺序：预算帧在最前，压缩帧紧跟触发它的框架事件之后。"""

    class E:
        def __init__(self, kind: str) -> None:
            self.type = kind

        def model_dump(self, mode: str = "json") -> dict[str, Any]:
            return {"type": self.type}

    sink = CompressionSink()

    class StreamingAgent(FakeAgent):
        def __init__(self) -> None:
            super().__init__(tokens=100)
            self._agent = self

        async def reply_stream(self, msg: object) -> AsyncIterator[object]:
            # 模拟压缩钩子在这次事件的处理过程中写入
            sink.emit({"type": CONTEXT_COMPRESSION_START, "before_tokens": 103053})
            yield E("REPLY_START")
            yield E("REPLY_END")

    runtime = AgentScopeRuntime(
        StreamingAgent(),  # type: ignore[arg-type]
        compression_sink=sink,
    )
    frames = _run(_collect(runtime.stream("s", "q")))
    assert [frame["type"] for frame in frames] == [
        CONTEXT_BUDGET,
        "REPLY_START",
        CONTEXT_COMPRESSION_START,
        "REPLY_END",
    ]


def test_stream_without_sink_keeps_event_sequence_unchanged() -> None:
    """Echo/测试替身不挂 sink 时不得凭空多出预算帧。"""

    class E:
        def __init__(self, kind: str) -> None:
            self.type = kind

        def model_dump(self, mode: str = "json") -> dict[str, Any]:
            return {"type": self.type}

    class Plain:
        async def reply_stream(self, msg: object) -> AsyncIterator[object]:
            yield E("REPLY_START")

    runtime = AgentScopeRuntime(Plain())  # type: ignore[arg-type]
    frames = _run(_collect(runtime.stream("s", "q")))
    assert [frame["type"] for frame in frames] == ["REPLY_START"]


async def _collect(source: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame async for frame in source]


def test_build_runtime_wires_compression_middleware(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """装配层必须真的把中间件挂到 Agent 上，并且运行时持有同一个 sink。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")

    runtime = build_agentscope_runtime(workdir=tmp_path)
    agent: Agent = runtime._agent
    assert any(mw.__class__ is CompressionMiddleware for mw in agent._compress_context_middlewares)
    assert runtime._compression_sink is not None


@pytest.mark.parametrize("ratio", [0.5, 0.9])
def test_context_budget_respects_custom_trigger_ratio(ratio: float) -> None:
    """阈值不是写死的：换 trigger_ratio 后前端拿到的数字必须跟着变。"""
    agent = FakeAgent()
    agent.context_config = ContextConfig(trigger_ratio=ratio)
    assert context_budget(agent)["threshold"] == int(ratio * 128000)  # type: ignore[arg-type]
