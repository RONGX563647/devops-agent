"""配置与模型参数的健康计数 —— 读路径降级时留下痕迹。

为什么要它：配置库读不出来时，所有域都会**静默**回落到代码默认值。界面上看是
「部署者从没调过参」，日志里只有一条 warning，排障时无法区分「没配」和「配了但
读不到」。与记忆层的 `memory/health.py` 同构：只记当下状态，不落库、不重试、
不参与任何判定。

按来源分桶（`db` = 配置库，`model` = 模型参数用的 settings 表），因为两者的
失败互不影响：配置库正常时不该把「模型库坏了」的标记清掉。

`degraded` 只在**最近一次**读取失败时为真：成功一次即清零，因为这里回答的是
「现在还能不能读到」，历史失败由日志回答。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_LOCK = threading.Lock()


@dataclass
class LayerHealth:
    """单层的失败计数：`last_reason` 非空即「正在降级」。"""

    failures: int = 0
    last_reason: str = ""
    last_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "last_reason": self.last_reason,
            "last_at": self.last_at,
        }


_LAYERS: dict[str, LayerHealth] = {}


def note_failure(layer: str, reason: str) -> None:
    """记一次读失败：`reason` 用 `类型: 消息`，便于直接贴进 issue。"""
    with _LOCK:
        item = _LAYERS.setdefault(layer, LayerHealth())
        item.failures += 1
        item.last_reason = reason
        item.last_at = datetime.now(UTC).isoformat(timespec="seconds")


def note_success(layer: str) -> None:
    """记一次成功：清掉该层的降级标记，保留累计失败次数。"""
    with _LOCK:
        _LAYERS.setdefault(layer, LayerHealth()).last_reason = ""


def snapshot() -> dict[str, Any]:
    """当前状态：任一层的 `last_reason` 非空即 `degraded`。"""
    with _LOCK:
        layers = {name: item.as_dict() for name, item in _LAYERS.items()}
        error = next((item["last_reason"] for item in layers.values() if item["last_reason"]), "")
    return {
        "degraded": bool(error),
        "error": error,
        "layers": layers,
    }


def reset() -> None:
    """测试用：清空全部计数。"""
    with _LOCK:
        _LAYERS.clear()


__all__ = ["LayerHealth", "note_failure", "note_success", "reset", "snapshot"]
