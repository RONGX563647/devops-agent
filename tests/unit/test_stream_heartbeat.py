"""SSE 周期心跳测试：长静默窗口内必须有注释帧保活，前端 watchdog 才不会误判。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from devops_agent import main


class _Recorder:
    """_frames 只用到 consume，最小桩即可。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def consume(self, event: dict[str, Any]) -> None:
        self.events.append(event)


async def _slow_source() -> AsyncIterator[dict[str, Any]]:
    """两次事件之间静默 0.1s，远大于被 patch 后的心跳间隔。"""
    await asyncio.sleep(0.1)
    yield {"type": "FIRST", "n": 1}
    await asyncio.sleep(0.1)
    yield {"type": "SECOND", "n": 2}


def test_frames_emits_periodic_heartbeat_during_silence(monkeypatch: Any) -> None:
    monkeypatch.setattr(main, "SSE_HEARTBEAT_S", 0.02)
    recorder = _Recorder()

    async def run() -> list[str]:
        return [frame async for frame in main._frames(_slow_source(), recorder)]  # type: ignore[arg-type]

    frames = asyncio.run(run())
    pings = [f for f in frames if f == ": ping\n\n"]
    data = [f for f in frames if f.startswith("data: ")]
    assert len(data) == 2, "两个真实事件都不能丢"
    assert len(pings) >= 2, "两段静默窗口内必须有多帧注释心跳"
    assert len(recorder.events) == 2, "心跳帧不能被 recorder 当成事件落库"


def test_frames_propagates_source_error(monkeypatch: Any) -> None:
    """上游异常必须冒泡，由外层 generate 记 failed，不能被心跳逻辑吞掉。"""
    monkeypatch.setattr(main, "SSE_HEARTBEAT_S", 0.02)

    async def broken_source() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "OK"}
        raise RuntimeError("boom")

    async def run() -> list[str]:
        return [frame async for frame in main._frames(broken_source(), _Recorder())]  # type: ignore[arg-type]

    import pytest

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())
