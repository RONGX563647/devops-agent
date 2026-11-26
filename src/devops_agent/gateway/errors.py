"""错误码与状态码的唯一映射。

改这里等于改前端契约：`ErrorCode` 的值就是 `detail`，`STATUS_OF` 决定重试语义。
口径固定为 —— 400 请求自相矛盾 / 403 越权或未开放 / 404 不存在 / 409 当前状态不允许。
「每码一状态、值不重复」由 `tests/unit/test_gateway_contract.py` 的表驱动用例钉住。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from fastapi import HTTPException


class ErrorCode(StrEnum):
    """网关对外返回的 detail 取值（同时也是前端的分支键）。"""

    # --- 权限档位 -------------------------------------------------------
    # 档位不存在或本部署未开放；不静默降级到默认档
    PERMISSION_NOT_AVAILABLE = "permission_not_available"

    # --- 会话并发 -------------------------------------------------------
    # 同一会话已有 run 在跑（SSE 流整段持有锁）
    SESSION_BUSY = "session_busy"
    # 资源不属于请求声称的会话（越权读别人待执行的命令）
    SESSION_MISMATCH = "session_mismatch"
    # 请求自相矛盾：路径里的 session_id 与 body 里的不一致
    REQUEST_MISMATCH = "request_mismatch"

    # --- 挂起态 ---------------------------------------------------------
    RUN_NOT_FOUND = "run_not_found"
    # 已被别的请求（双击、另一个标签页）确认过，或本来就不是挂起态
    ALREADY_RESOLVED = "already_resolved"
    EXPIRED = "expired"
    EMPTY_RESULTS = "empty_results"
    # 运行时没有续跑能力（只实现了 stream 的替身）
    RUNTIME_NOT_RESUMABLE = "runtime_not_resumable"


# 一条码恰好一个状态码；口径见模块 docstring
STATUS_OF: Final[dict[ErrorCode, int]] = {
    ErrorCode.PERMISSION_NOT_AVAILABLE: 403,
    ErrorCode.SESSION_BUSY: 409,
    ErrorCode.SESSION_MISMATCH: 403,
    ErrorCode.REQUEST_MISMATCH: 400,
    ErrorCode.RUN_NOT_FOUND: 404,
    ErrorCode.ALREADY_RESOLVED: 409,
    ErrorCode.EXPIRED: 409,
    ErrorCode.EMPTY_RESULTS: 400,
    ErrorCode.RUNTIME_NOT_RESUMABLE: 400,
}


def http_error(code: ErrorCode) -> HTTPException:
    """按错误码构造 HTTPException（业务代码不再出现裸数字状态码）。"""
    return HTTPException(status_code=STATUS_OF[code], detail=code.value)
