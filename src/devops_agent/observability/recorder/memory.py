"""记忆事件 → span 的纯函数；recorder 只负责序号与落库。

单独成文件是为了让 recorder.py 保持在 500 行以内，逻辑本身仍只依赖 models。
"""

from __future__ import annotations

import json
from typing import Any

from devops_agent.observability.models import SpanRecord
from devops_agent.observability.recorder.usage import _as_int

MEMORY_SPAN_NAMES = {
    "MEMORY_RETRIEVE_END": "memory.retrieve",
    "MEMORY_TRIM": "memory.trim",
    "MEMORY_COMPRESS_END": "memory.compress",
    "MEMORY_WRITE_END": "memory.write",
}

_MAX_TEXT = 20_000


def _detail(event: dict[str, object]) -> dict[str, Any]:
    """事件明细统一在 `memory` 子对象里；顶层字段只是旧帧的兼容回退。"""
    raw = event.get("memory")
    detail: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    detail["state"] = str(event.get("state", "ok") or "ok")
    reason = str(event.get("reason", "") or "")
    if reason:
        detail["reason"] = reason
    return detail


def _nested(detail: dict[str, Any], event: dict[str, object], key: str) -> Any:
    """先读明细，再回落顶层：只读一处会让老帧和新帧各自为政。"""
    value = detail.get(key)
    return value if value not in (None, "") else event.get(key)


def memory_span(
    event: dict[str, object], *, run_id: int, seq: int, start_offset_ms: int
) -> SpanRecord | None:
    """把一帧记忆事件折成 span；跳过态也留痕，回答「为什么没记忆」。"""
    name = MEMORY_SPAN_NAMES.get(str(event.get("type", "")))
    if name is None:
        return None
    detail = _detail(event)
    status = "error" if detail["state"] == "error" else "ok"
    reason = str(detail.get("reason", ""))
    return SpanRecord(
        id=None,
        run_id=run_id,
        type="memory",
        name=name,
        start_offset_ms=start_offset_ms,
        duration_ms=_as_int(_nested(detail, event, "latency_ms") or 0),
        input=str(_nested(detail, event, "query") or reason)[:_MAX_TEXT],
        output=json.dumps(detail, ensure_ascii=False)[:_MAX_TEXT],
        tokens=0,
        status=status,
        error=reason if status == "error" and reason else None,
        seq=seq,
        parent_id=None,
    )
