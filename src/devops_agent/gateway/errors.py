"""HTTP 错误码 —— 状态码与错误码的唯一事实源。

为什么单独成模块：同一件事只允许有一份定义。改造前是 10 处
`raise HTTPException(status_code=..., detail="...")`，其中 `session_busy` 与
`session_mismatch` 各自在两个文件里各写一遍 —— 加一个码或改一个状态码只能靠人肉搜索。

分工：
- `ErrorCode` 是**对外契约**：前端按 code 决定文案，按状态码决定重试策略，因此不可配置；
- `STATUS_OF` 是唯一映射表，一条 `ErrorCode` 恰好一个状态码；
- `http_error()` 是唯一抛出入口，业务代码不再出现裸数字状态码。

一致性由 tests/unit/test_gateway_contract.py 的表驱动用例钉住（每个成员都有映射且不重复）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from fastapi import HTTPException


class ErrorCode(StrEnum):
    """网关对外返回的 detail 取值（同时也是前端的分支键）。"""

    # --- 权限档位 -------------------------------------------------------
    # 请求了不存在或本部署未开放的档位；不静默降级到默认档
    PERMISSION_NOT_AVAILABLE = "permission_not_available"

    # --- 会话并发 -------------------------------------------------------
    # 同一会话已有 run 在跑（SSE 流整段持有锁）
    SESSION_BUSY = "session_busy"
    # 请求声称的会话与该资源的归属不符（越权读别人待执行的命令）
    SESSION_MISMATCH = "session_mismatch"
    # 请求自相矛盾：路径里的 session_id 与 body 里的不一致
    REQUEST_MISMATCH = "request_mismatch"

    # --- 挂起态 ---------------------------------------------------------
    RUN_NOT_FOUND = "run_not_found"
    # 已被别的请求（双击、另一个标签页）确认过，或不是挂起态
    ALREADY_RESOLVED = "already_resolved"
    EXPIRED = "expired"
    EMPTY_RESULTS = "empty_results"
    # 运行时没有 resume 能力（Echo / 只实现了 stream 的替身）
    RUNTIME_NOT_RESUMABLE = "runtime_not_resumable"


# 状态码选择的口径：
# - 403 = 你无权/未开放；409 = 当前状态不允许这次操作；400 = 请求本身自相矛盾；
# - 404 只用于「这条 run 根本不存在」，与「存在但已结清」严格区分（后者是 409）。
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
    """按错误码构造 HTTPException：状态码查表，detail 用码本身。"""
    return HTTPException(status_code=STATUS_OF[code], detail=code.value)
