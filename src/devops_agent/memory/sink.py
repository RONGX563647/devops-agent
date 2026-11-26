"""记忆帧队列 —— 与 CompressionSink 同构：钩子写，流读取。"""

from __future__ import annotations

from collections import deque
from typing import Any


class MemorySink:
    """运行时持有的帧队列；deque 足够，钩子与读取都在同一事件循环里。"""

    def __init__(self) -> None:
        self._frames: deque[dict[str, Any]] = deque()

    def emit(self, frame: dict[str, Any]) -> None:
        self._frames.append(frame)

    def drain(self) -> list[dict[str, Any]]:
        frames = list(self._frames)
        self._frames.clear()
        return frames
