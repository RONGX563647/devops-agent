"""SSE 帧编码与心跳泵：把运行时事件流翻译成前端可直接消费的帧。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from devops_agent.observability.recorder import RunRecorder

# 心跳间隔：静默超过该秒数仍在流中时补一帧注释，避免前端 watchdog 误判假死。
SSE_HEARTBEAT_S = 15

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse_frame(payload: dict[str, Any]) -> str:
    """把一个 dict 包装成标准 SSE data 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _named_frame(name: str, payload: dict[str, Any]) -> str:
    """带 event 名的 SSE 帧（done / parked）。"""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _frames(
    source: AsyncIterator[dict[str, Any]],
    recorder: RunRecorder,
    *,
    heartbeat: float | None = None,
) -> AsyncIterator[str]:
    """把运行时事件流翻译成 SSE 帧，同时喂给 recorder 落库。

    模型首包、长工具调用都可能静默几十秒。前端 watchdog 按「多久没收到字节」
    判假死，因此这里在静默超过心跳间隔时补一帧 SSE 注释（`: ping`），
    让连接保持有字节流动。用独立的 pump/beat 双任务 + 队列，而不是
    `wait_for(source.__anext__(), timeout=...)`：后者超时会取消上游生成器的
    `__anext__`，可能永久打断 AgentScope 的事件流。

    `heartbeat` 省略时用模块级 `SSE_HEARTBEAT_S`；显式传入供兼容门面按自己的
    常量驱动（测试会替换门面模块上的间隔）。
    """
    interval = SSE_HEARTBEAT_S if heartbeat is None else heartbeat
    # 队列元素：（类型标记, 负载）。异常用单独标记传回并在本生成器里重新抛出，
    # 保持原语义——由外层 generate() 的 except 记 failed 并落库。
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in source:
                recorder.consume(event)
                await queue.put(("frame", _sse_frame(event)))
        except BaseException as exc:  # 透传给下游重新抛出，不能静默吞掉
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", None))

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            await queue.put(("frame", ": ping\n\n"))

    pump_task = asyncio.create_task(pump())
    beat_task = asyncio.create_task(beat())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "done":
                break
            if kind == "error":
                raise payload
            yield payload
    finally:
        # 上游结束或客户端断开时，两个后台任务都必须收掉，避免泄漏。
        for task in (pump_task, beat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(pump_task, beat_task, return_exceptions=True)
