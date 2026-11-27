"""记忆健康计数 —— 回答「现在坏没坏」，不参与任何判定。

记忆的每次失败都被降级成空值（不阻断对话），这是设计；代价是故障不可见：
`/api/memory/status` 只看开关，日志干净，记忆却已静默停摆。这里用进程内计数
补上「最近有没有连续失败」这一层，不落库、不重试、不影响主流程。

为什么不做持久化：它要回答的是当下状态；跨重启的历史由 spans 表回答。
计数按层（episodic / long_term / turn）分桶，一次成功即清零 `consecutive`，
`failures` 保留累计值供排查。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_LOCK = threading.Lock()


@dataclass
class LayerHealth:
    """单层的失败计数：`consecutive` 决定「是否正在降级」。"""

    failures: int = 0
    consecutive: int = 0
    last_reason: str = ""
    last_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "consecutive": self.consecutive,
            "last_reason": self.last_reason,
            "last_at": self.last_at,
        }


_LAYERS: dict[str, LayerHealth] = {}


def _entry(layer: str) -> LayerHealth:
    return _LAYERS.setdefault(layer, LayerHealth())


def note_failure(layer: str, reason: str) -> None:
    """记一次降级：层名用存储层（episodic / long_term / turn）。"""
    with _LOCK:
        item = _entry(layer)
        item.failures += 1
        item.consecutive += 1
        item.last_reason = reason
        item.last_at = datetime.now(UTC).isoformat(timespec="seconds")


def note_success(layer: str) -> None:
    """记一次成功：清零连续失败，但保留累计次数。"""
    with _LOCK:
        item = _entry(layer)
        item.consecutive = 0


def snapshot() -> dict[str, Any]:
    """当前健康快照：`degraded` 只看连续失败，历史失败看 `layers`。"""
    with _LOCK:
        layers = {name: item.as_dict() for name, item in _LAYERS.items()}
    return {
        "degraded": any(item["consecutive"] > 0 for item in layers.values()),
        "layers": layers,
    }


def reset() -> None:
    """测试用：清空全部计数。"""
    with _LOCK:
        _LAYERS.clear()
