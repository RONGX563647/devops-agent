"""SSE 帧编码与心跳泵：把运行时事件流翻译成前端可直接消费的帧。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from devops_agent.observability.recorder import RunRecorder

# 队列元素：（类型标记, 负载）。异常用单独标记传回并在下游重新抛出，
# 保持「上游异常冒泡给 generate()」的原语义。
_Chunk = tuple[str, Any]

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


async def _pump_events(
    source: AsyncIterator[dict[str, Any]], recorder: RunRecorder, queue: asyncio.Queue[_Chunk]
) -> None:
    """把上游事件逐条转成 SSE 帧写入队列；异常与结束都以标记投递。"""
    try:
        async for event in source:
            recorder.consume(event)
            await queue.put(("frame", _sse_frame(event)))
    except BaseException as exc:  # 透传给下游重新抛出，不能静默吞掉
        await queue.put(("error", exc))
    finally:
        await queue.put(("done", None))


async def _heartbeat(queue: asyncio.Queue[_Chunk], interval: float) -> None:
    """静默补帧：每隔 interval 投一帧 SSE 注释，让连接始终有字节流动。"""
    while True:
        await asyncio.sleep(interval)
        await queue.put(("frame", ": ping\n\n"))


async def _drain(queue: asyncio.Queue[_Chunk]) -> AsyncIterator[str]:
    """消费队列直到结束标记；异常标记在这里重新抛出。"""
    while True:
        kind, payload = await queue.get()
        if kind == "done":
            return
        if kind == "error":
            raise payload
        yield payload


async def _frames(
    source: AsyncIterator[dict[str, Any]],
    recorder: RunRecorder,
    *,
    heartbeat: float | None = None,
) -> AsyncIterator[str]:
    """把运行时事件流翻译成 SSE 帧，同时喂给 recorder 落库。

    用独立的 pump/heartbeat 双任务 + 队列，而不是
    `wait_for(source.__anext__(), timeout=...)`：后者超时会取消上游生成器的
    `__anext__`，可能永久打断 AgentScope 的事件流。

    1) 起两个后台任务：pump 转帧、heartbeat 在静默期补注释帧；
    2) 消费队列，逐帧下发（异常在 _drain 里重新抛出）；
    3) finally 取消并回收两个任务，避免上游结束或客户端断开时泄漏。

    `heartbeat` 省略时用模块级 `SSE_HEARTBEAT_S`；显式传入供调用方自定义间隔。
    """
    interval = SSE_HEARTBEAT_S if heartbeat is None else heartbeat
    queue: asyncio.Queue[_Chunk] = asyncio.Queue()
    pump_task = asyncio.create_task(_pump_events(source, recorder, queue))
    beat_task = asyncio.create_task(_heartbeat(queue, interval))
    try:
        async for frame in _drain(queue):
            yield frame
    finally:
        for task in (pump_task, beat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(pump_task, beat_task, return_exceptions=True)
