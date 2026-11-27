"""记忆中间件、压缩联动与落库测试（SPEC-0013 §5.5 / §6.7 / §8.3）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from agentscope.agent import ContextConfig
from agentscope.message import Msg, SystemMsg, TextBlock, UserMsg

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.observer import MemoryMiddleware
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.assembly import build_agentscope_runtime
from devops_agent.runtime.compression import CompressionMiddleware, CompressionSink
from devops_agent.runtime.events import event_to_dict
from devops_agent.runtime.scope_runtime import AgentScopeRuntime


class FakeModel:
    context_size = 128000

    def __init__(self, tokens: int = 100) -> None:
        self.tokens = tokens

    async def count_tokens(self, **_kwargs: Any) -> int:
        return self.tokens


class FakeState:
    def __init__(self) -> None:
        self.summary: str = ""
        self.context: list[Any] = []
        self.middle_context: dict[str, Any] = {}
        self.reply_id = "r1"


class FakeAgent:
    def __init__(self) -> None:
        self.model = FakeModel()
        self.state = FakeState()
        self.context_config = ContextConfig()

    async def _prepare_model_input(self) -> dict[str, Any]:
        return {"messages": []}


def _runtime(tmp_path: Any, session: str = "s1") -> MemoryRuntime:
    return MemoryRuntime(session, load_memory(), MemorySink(), tmp_path / "obs.db")


def _text(msg: Msg) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    return "\n".join(getattr(block, "text", "") for block in content)


def test_injects_episodic_note_once_per_reply(tmp_path) -> None:  # type: ignore[no-untyped-def]
    runtime = _runtime(tmp_path)
    runtime.episodic.save(
        "s1", text="目标：修好登录", trigger="t", tokens_before=10, tokens_after=5, budget=8
    )
    middleware = MemoryMiddleware(runtime)
    agent = FakeAgent()
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]

    captured: dict[str, Any] = {}

    async def next_handler(**kwargs: Any) -> str:
        captured["messages"] = kwargs["messages"]
        return "ok"

    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    frames = runtime.sink.drain()
    kinds = [frame["type"] for frame in frames]
    assert events.MEMORY_RETRIEVE_END in kinds
    assert events.CONTEXT_ASSEMBLED in kinds
    note = _text(captured["messages"][-1])
    assert "会话记忆" in note and "修好登录" in note

    # 同一 reply 的第二次模型调用：note 继续带上，但不再重复发检索事件
    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    assert runtime.sink.drain() == []


def test_skips_injection_when_version_unchanged_across_turns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    runtime = _runtime(tmp_path)
    runtime.episodic.save(
        "s1", text="目标：修好登录", trigger="t", tokens_before=10, tokens_after=5, budget=8
    )
    middleware = MemoryMiddleware(runtime)
    agent = FakeAgent()
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]

    async def next_handler(**kwargs: Any) -> str:
        return "ok"

    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    runtime.sink.drain()
    agent.state.reply_id = "r2"
    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    frames = runtime.sink.drain()
    retrieve = [frame for frame in frames if frame["type"] == events.MEMORY_RETRIEVE_END]
    assert retrieve and retrieve[0]["state"] == "skipped"


def test_compression_writes_episodic_version(tmp_path) -> None:  # type: ignore[no-untyped-def]
    runtime = _runtime(tmp_path)
    sink = CompressionSink()
    middleware = CompressionMiddleware(sink, runtime)
    agent = FakeAgent()

    async def next_handler(**_kwargs: Any) -> None:
        agent.state.summary = "压缩后的摘要"
        agent.model.tokens = 40

    asyncio.run(middleware.on_compress_context(agent, {}, next_handler))
    frames = runtime.sink.drain()
    kinds = [frame["type"] for frame in frames]
    assert events.MEMORY_COMPRESS_END in kinds
    assert events.MEMORY_WRITE_END in kinds
    active = runtime.episodic.active("s1")
    assert active is not None and active["text"] == "压缩后的摘要"
    assert active["tokens_after"] == 40


def test_compression_failure_keeps_old_card(tmp_path) -> None:  # type: ignore[no-untyped-def]
    runtime = _runtime(tmp_path)
    middleware = CompressionMiddleware(CompressionSink(), runtime)
    agent = FakeAgent()

    async def next_handler(**_kwargs: Any) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(middleware.on_compress_context(agent, {}, next_handler))
    end = [f for f in runtime.sink.drain() if f["type"] == events.MEMORY_COMPRESS_END]
    assert end and end[0]["state"] == "error"


def test_reply_end_writes_long_term(tmp_path) -> None:  # type: ignore[no-untyped-def]
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = FakeAgent()
    agent.state.context = [
        Msg(name="devops", role="assistant", content=[TextBlock(type="text", text="好的")])
    ]

    async def source() -> AsyncGenerator[dict[str, Any], None]:
        yield {"type": "REPLY_END"}

    async def collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in middleware.on_reply(
                agent,
                {"inputs": UserMsg(name="s1", content="请记住我更喜欢 pytest")},
                source,  # type: ignore[arg-type]
            )
        ]

    assert asyncio.run(collect()) == [{"type": "REPLY_END"}]
    assert runtime.sink.drain()
    hits = asyncio.run(runtime.long_term.search("pytest", top_k=5, min_score=0.1))
    assert hits


def test_recorder_persists_memory_spans_and_projection_keeps_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "obs.db"
    recorder = RunRecorder(db)
    recorder.start("s1", "q")
    recorder.consume(
        {
            "type": events.MEMORY_TRIM,
            "state": "ok",
            "memory": {"dropped_turns": 2, "reclaimed_chars": 10},
        }
    )
    recorder.consume(
        {
            "type": events.MEMORY_COMPRESS_END,
            "state": "error",
            "reason": "boom",
            "memory": {"tokens_before": 100, "tokens_after": 40},
        }
    )
    run_id = recorder.finish()
    with repo.connect(db) as conn:
        spans = repo.list_spans(conn, run_id)
    memory_spans = [span for span in spans if span.type == "memory"]
    assert [span.name for span in memory_spans] == ["memory.trim", "memory.compress"]
    assert memory_spans[1].status == "error"
    assert memory_spans[1].error == "boom"

    class Fake:
        type = events.MEMORY_TRIM
        layer = "short"
        state = "ok"
        reason = ""
        memory = {"dropped_turns": 1}

    projected = event_to_dict(Fake())
    assert projected["memory"] == {"dropped_turns": 1}


def test_snapshot_caps_history_and_stream_drains_memory_frames(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """快照体积保护 + 运行时按序转发记忆帧。"""
    runtime = _runtime(tmp_path)
    runtime.policy = runtime.policy.model_copy(
        update={"context": runtime.policy.context.model_copy(update={"snapshot_max_chars": 200})}
    )
    agent = FakeAgent()
    agent.state.context = [UserMsg(name="s1", content="x" * 500) for _ in range(4)]
    runtime.cap_snapshot(agent)
    assert len(agent.state.context) < 4
    # 快照裁剪现在会发事件（M6）：它属于「保存时」而不是本轮请求，先取走再测流式转发
    assert [f["reason"] for f in runtime.sink.drain()] == ["snapshot_cap"]

    class Streaming:
        def __init__(self) -> None:
            self.state = FakeState()

        async def reply_stream(self, _msg: object) -> AsyncGenerator[Any, None]:
            runtime.sink.emit(events.frame(events.MEMORY_TRIM, layer="short"))
            yield type("E", (), {"model_dump": lambda self, mode="json": {"type": "REPLY_END"}})()

    async def collect() -> list[dict[str, Any]]:
        scope = AgentScopeRuntime(Streaming(), memory=runtime)  # type: ignore[arg-type]
        return [frame async for frame in scope.stream("s1", "q")]

    frames = asyncio.run(collect())
    assert [frame["type"] for frame in frames] == ["REPLY_END", events.MEMORY_TRIM]


def test_build_runtime_wires_memory_only_with_session_id(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    runtime = build_agentscope_runtime(workdir=tmp_path, session_id="s1")
    assert runtime._memory is not None  # type: ignore[attr-defined]
    assert any(
        mw.__class__ is MemoryMiddleware
        for mw in runtime._agent._model_call_middlewares  # type: ignore[attr-defined]
    )
    plain = build_agentscope_runtime(workdir=tmp_path)
    assert plain._memory is None  # type: ignore[attr-defined]


def test_note_body_not_truncated_by_trace_preview(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A18：注入正文按「卡片上限 ∩ 中期预算」，不再被 160 字的轨迹预览截断。"""
    runtime = _runtime(tmp_path)
    body = "目标：修好登录。" + "细节" * 300
    runtime.episodic.save("s1", text=body, trigger="t", tokens_before=10, tokens_after=5, budget=8)
    note = runtime.note(runtime.episodic.active("s1"), [])
    preview = runtime.policy.trace.summary_preview_chars
    assert len(note) > preview * 2
    assert body[:300] in note


def test_retrieval_failure_reports_error_not_miss(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A22：后端抛异常时 state=error，reason 带异常类名；miss 只表示真的没有。"""

    class Boom:
        name = "local"

        async def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("db locked")

        async def write(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"stored": False, "deduped": False}

    runtime = _runtime(tmp_path)
    runtime.long_term = Boom()
    middleware = MemoryMiddleware(runtime)
    agent = FakeAgent()

    async def next_handler(**_kwargs: Any) -> str:
        return "ok"

    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]
    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    end = [f for f in runtime.sink.drain() if f["type"] == events.MEMORY_RETRIEVE_END]
    assert end and end[0]["state"] == "error"
    assert "RuntimeError" in end[0]["reason"]


def test_empty_reply_id_assembles_once_per_turn(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A21：reply_id 为空时，同一轮内两次模型调用只装配一次、只注入一次。"""
    runtime = _runtime(tmp_path)
    runtime.episodic.save(
        "s1", text="目标：修好登录", trigger="t", tokens_before=10, tokens_after=5, budget=8
    )
    middleware = MemoryMiddleware(runtime)
    agent = FakeAgent()
    agent.state.reply_id = ""
    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]
    seen: list[Any] = []

    async def next_handler(**kwargs: Any) -> str:
        seen.append(kwargs["messages"][-1])
        return "ok"

    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    frames = runtime.sink.drain()
    assert [f["type"] for f in frames].count(events.MEMORY_RETRIEVE_END) == 1
    assert [f["type"] for f in frames].count(events.CONTEXT_ASSEMBLED) == 1

    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, next_handler))
    assert runtime.sink.drain() == []
    assert _text(seen[0]) == _text(seen[1])
