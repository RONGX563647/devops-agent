"""记忆事件契约 —— 事件名与帧构造的唯一来源。

字段放在结构化的 `memory` 对象里，顶层只保留 `type` / `layer` / `state` /
`reason`，让 recorder 与前端都能按同一套键消费；只放预览，不放全文。
"""

from __future__ import annotations

from typing import Any

MEMORY_RETRIEVE_START = "MEMORY_RETRIEVE_START"
MEMORY_RETRIEVE_END = "MEMORY_RETRIEVE_END"
MEMORY_TRIM = "MEMORY_TRIM"
MEMORY_COMPRESS_START = "MEMORY_COMPRESS_START"
MEMORY_COMPRESS_END = "MEMORY_COMPRESS_END"
MEMORY_WRITE_START = "MEMORY_WRITE_START"
MEMORY_WRITE_END = "MEMORY_WRITE_END"
CONTEXT_ASSEMBLED = "CONTEXT_ASSEMBLED"

# 没有 CONTEXT_RESTORE：恢复发生在装配期，那时还没有 SSE 流可以承载事件；
# 恢复失败改为结构化日志 + `/health` 计数（SPEC-0013 §18.2 M5）。

# 需要落成 span 的事件：START 只用于配对，不单独落库
SPAN_MEMORY_EVENTS = frozenset(
    {MEMORY_RETRIEVE_END, MEMORY_TRIM, MEMORY_COMPRESS_END, MEMORY_WRITE_END}
)

MEMORY_EVENT_TYPES = frozenset(
    {
        MEMORY_RETRIEVE_START,
        MEMORY_RETRIEVE_END,
        MEMORY_TRIM,
        MEMORY_COMPRESS_START,
        MEMORY_COMPRESS_END,
        MEMORY_WRITE_START,
        MEMORY_WRITE_END,
        CONTEXT_ASSEMBLED,
    }
)


def frame(
    kind: str,
    *,
    layer: str = "",
    state: str = "ok",
    reason: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """构造一帧记忆/上下文事件；细节统一收进 `memory`。"""
    payload = {"type": kind, "layer": layer, "state": state, "reason": reason, "memory": fields}
    return payload
