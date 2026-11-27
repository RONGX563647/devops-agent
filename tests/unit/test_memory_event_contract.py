"""事件与接口契约（SPEC-0016 §10 A34）。

预算执行与前缀缓存都是「看不见的行为」：裁剪了多少、为什么缓存失效、估算漂移
到哪去了 —— 只有事件字段和 `/health` 上能看见才算真的实现。这组断言钉住字段名，
因为前端与诊断脚本按名字取数，改名等于静默丢功能。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from agentscope.agent import ContextConfig
from agentscope.message import Msg, SystemMsg, ToolResultBlock, UserMsg
from fastapi.testclient import TestClient

from devops_agent.config import load_memory
from devops_agent.gateway.app import app
from devops_agent.memory import events
from devops_agent.memory.observer import MemoryMiddleware
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink

TOOLS = [{"name": "Bash", "description": "run", "parameters": {"type": "object"}}]


class _Model:
    def __init__(self, context_size: int = 20000) -> None:
        self.context_size = context_size

    async def count_tokens(self, **_kwargs: Any) -> int:
        return 100


class _Agent:
    def __init__(self) -> None:
        self.model = _Model()
        self.state = SimpleNamespace(
            summary="", context=[], middle_context={}, reply_id="r1"
        )
        self.context_config = ContextConfig()

    async def _prepare_model_input(self) -> dict[str, Any]:
        return {"messages": []}


def _heavy_messages(count: int = 40) -> list[Msg]:
    messages: list[Msg] = [SystemMsg(name="system", content="sys")]
    for index in range(count):
        messages.append(UserMsg(name="u", content=f"q{index}"))
        messages.append(
            Msg(
                name="tool",
                role="assistant",
                content=[ToolResultBlock(id=f"id{index}", name=f"t{index}", output="y" * 4000)],
            )
        )
    return messages


def _frames(tmp_path: Any) -> tuple[list[dict[str, Any]], MemoryRuntime]:
    runtime = MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")
    middleware = MemoryMiddleware(runtime)
    agent = _Agent()
    messages = _heavy_messages()

    async def handler(**kwargs: Any) -> Any:
        return SimpleNamespace(usage=None)

    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": TOOLS}, handler))
    return runtime.sink.drain(), runtime


def test_memory_trim_reports_budget_fields(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """`MEMORY_TRIM` 必须带 `stage/limit/over_budget`：否则「裁了多少」只能靠猜。"""
    frames, _runtime = _frames(tmp_path)
    trims = [frame for frame in frames if frame["type"] == events.MEMORY_TRIM]
    assert trims, "超预算却没有裁剪事件"
    payload = trims[-1]["memory"]
    assert payload["stage"] in {"S1", "S2", "S3", "S4", "S5"}
    assert payload["limit"] > 0
    assert payload["tokens_before"] > payload["limit"]
    assert isinstance(payload["over_budget"], bool)
    assert trims[-1]["reason"] == events.REASON_REQUEST_TRIM


def test_context_assembled_carries_cache_section(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """`CONTEXT_ASSEMBLED` 的三段：zones / budget / cache，字段名按 SPEC §6.2。"""
    frames, _runtime = _frames(tmp_path)
    assembled = [frame for frame in frames if frame["type"] == events.CONTEXT_ASSEMBLED]
    assert len(assembled) == 1
    payload = assembled[0]["memory"]

    assert set(payload["zones"]) == {"prefix", "history", "tail", "output_reserve"}
    assert {"history_limit", "note_limit", "overshoot"} <= set(payload["budget"])
    cache = payload["cache"]
    assert set(cache) == {
        "prefix_fingerprint",
        "prefix_reused",
        "history_changed_at",
        "history_changed_by",
        "cached_prefix_tokens",
        "estimated",
    }
    assert len(cache["prefix_fingerprint"]) == 12
    assert cache["history_changed_by"] in {"none", "trim_tail", "dedup", "trim_mid", "drop_turns"}


def test_health_exposes_retrieval_and_token_drift() -> None:
    """/health 的 `memory` 段要能看见「用哪种检索」和「估算漂移到哪」。"""
    body = TestClient(app).get("/health").json()
    memory = body["memory"]
    assert memory["retrieval"] in {"bm25", "overlap"}
    assert memory["summarize"] in {"off", "extractive", "model"}
    assert "drift" in memory["tokens"]
    assert "family" in memory["tokens"]
    assert {"ok", "overshoot", "history_limit", "note_limit"} <= set(memory["budget"])
