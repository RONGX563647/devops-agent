"""应用入口（兼容门面）。

实现已按职责拆到 `devops_agent.gateway`：`app` / 路由 / SSE / 依赖解析。
本模块只保留原导入路径，`uvicorn devops_agent.main:app` 与既有 `from
devops_agent.main import ...` 均不受影响。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from devops_agent.gateway.app import (
    _warn_missing_tools,
    _warn_runtime,
    app,
    health,
    runtime_status,
    select_runtime,
)
from devops_agent.gateway.confirm import (
    _check_pending,
    _pending_row,
    _profile_for_run,
    confirm_run,
    get_pending,
)
from devops_agent.gateway.deps import _profile_or_403, _recorder_for, _registry
from devops_agent.gateway.input import _load_attachments, _prepare_input, _save_secret
from devops_agent.gateway.schemas import (
    ConfirmItem,
    ConfirmRequest,
    RunRequest,
    SessionPermissionRequest,
)
from devops_agent.gateway.sessions import list_permissions, switch_session_permission
from devops_agent.gateway.sse import (
    _SSE_HEADERS,
    SSE_HEARTBEAT_S,
    _named_frame,
    _sse_frame,
)
from devops_agent.gateway.sse import _frames as _sse_frames
from devops_agent.gateway.stream import (
    ASK_LIMIT,
    PARK_TTL_SECONDS,
    SETTLE_ROUNDS,
    _bump_asks,
    _expires_at,
    _park,
    _rejections,
    _settle_abandoned,
    _stream_entry,
    pending_of,
    run_stream,
)
from devops_agent.observability.recorder import RunRecorder

__all__ = [
    "ASK_LIMIT",
    "PARK_TTL_SECONDS",
    "SETTLE_ROUNDS",
    "SSE_HEARTBEAT_S",
    "ConfirmItem",
    "ConfirmRequest",
    "RunRequest",
    "SessionPermissionRequest",
    "_SSE_HEADERS",
    "_bump_asks",
    "_check_pending",
    "_expires_at",
    "_frames",
    "_load_attachments",
    "_named_frame",
    "_park",
    "_pending_row",
    "_prepare_input",
    "_profile_for_run",
    "_profile_or_403",
    "_recorder_for",
    "_registry",
    "_rejections",
    "_save_secret",
    "_settle_abandoned",
    "_sse_frame",
    "_stream_entry",
    "_warn_missing_tools",
    "_warn_runtime",
    "app",
    "confirm_run",
    "get_pending",
    "health",
    "list_permissions",
    "pending_of",
    "run_stream",
    "runtime_status",
    "select_runtime",
    "switch_session_permission",
]


async def _frames(
    source: AsyncIterator[dict[str, Any]], recorder: RunRecorder
) -> AsyncIterator[str]:
    """兼容入口：心跳间隔以本模块的 `SSE_HEARTBEAT_S` 为准。

    单测直接替换 `devops_agent.main.SSE_HEARTBEAT_S` 来压缩等待时间，
    因此这里显式把门面模块上的值透传给 SSE 实现。
    """
    async for frame in _sse_frames(source, recorder, heartbeat=SSE_HEARTBEAT_S):
        yield frame
