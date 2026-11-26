"""SSE 保活 —— 把运行时事件流翻成帧，并在静默期补心跳。

职责只有一个：**让连接始终有字节流动**，不要被代理或前端 watchdog 判成假死。
帧长什么样在 contract.py（本模块不拼字段，只搬帧）。

为什么是「pump + heartbeat 双任务 + 队列」而不是
`asyncio.wait_for(source.__anext__(), timeout=...)`：
后者超时会取消上游生成器的 `__anext__`，可能永久打断 AgentScope 的事件流。
心跳是「补帧」而不是「超时重试」，两者语义完全不同。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from devops_agent.gateway.contract import PING_FRAME, sse_frame
from devops_agent.observability.recorder import RunRecorder

# 队列元素：（类型标记, 负载）。异常用单独标记回传并在下游重新抛出，
# 保持「上游异常冒泡给调用方」的原语义。
_Chunk = tuple[str, Any]

# 心跳间隔的模块级默认值：调用方（run.py）通常显式传 limits.heartbeat_seconds，
# 这里保留一份默认，便于 sse 单测与「只想要个帧流」的脚本直接调用。
DEFAULT_HEARTBEAT_S = 15.0


async def _pump_events(
    source: AsyncIterator[dict[str, Any]], recorder: RunRecorder, queue: asyncio.Queue[_Chunk]
) -> None:
    """把上游事件逐条转成 SSE 帧写入队列；异常与结束都以标记投递。"""
    try:
        async for event in source:
            recorder.consume(event)
            await queue.put(("frame", sse_frame(event)))
    except BaseException as exc:  # noqa: BLE001 - 透传给下游重新抛出，不能静默吞掉
        await queue.put(("error", exc))
    finally:
        await queue.put(("done", None))


async def _heartbeat(queue: asyncio.Queue[_Chunk], interval: float) -> None:
    """静默补帧：每隔 interval 投一帧 SSE 注释。"""
    while True:
        await asyncio.sleep(interval)
        await queue.put(("frame", PING_FRAME))


async def _drain(queue: asyncio.Queue[_Chunk]) -> AsyncIterator[str]:
    """消费队列直到结束标记；异常标记在这里重新抛出。"""
    while True:
        kind, payload = await queue.get()
        if kind == "done":
            return
        if kind == "error":
            raise payload
        yield payload


async def frames(
    source: AsyncIterator[dict[str, Any]],
    recorder: RunRecorder,
    *,
    heartbeat: float | None = None,
) -> AsyncIterator[str]:
    """把运行时事件流翻译成 SSE 帧，同时喂给 recorder 落库。

    1) 起两个后台任务：pump 转帧、heartbeat 在静默期补注释帧；
    2) 消费队列，逐帧下发（异常在 _drain 里重新抛出）；
    3) finally 取消并回收两个任务，避免上游结束或客户端断开时泄漏
       —— 客户端断连抛的是 GeneratorExit / CancelledError，只有 finally 能兜住。
    """
    interval = DEFAULT_HEARTBEAT_S if heartbeat is None else heartbeat
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
