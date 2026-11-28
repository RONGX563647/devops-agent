"""工作区领域错误 —— 稳定错误码 + 域内上下文，不依赖网关。

网关把 `code` 映射成 HTTP 状态码（`gateway/routes/workspaces.py`）；本模块不 import
FastAPI，也不 import 网关，保证 `devops_agent.workspace` 可以在无框架环境里单测。
"""

from __future__ import annotations

from typing import Any


class WorkspaceError(Exception):
    """一个可被网关翻译成 4xx 的工作区错误。

    `code` 是稳定枚举（前端据此出文案），`message` 只给人看，两者不混用。
    `extra` 放结构化细节（占用者、脏文件列表等），网关原样放进响应体。
    """

    def __init__(self, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.extra = extra


# 错误码：与 gateway.errors.ErrorCode 的同名成员逐字一致（测试钉住）
DISABLED = "workspace_disabled"
NOT_FOUND = "workspace_not_found"
NAME_TAKEN = "workspace_name_taken"
PATH_TAKEN = "workspace_path_taken"
LOCAL_DISABLED = "workspace_local_disabled"
GIT_DISABLED = "workspace_git_disabled"
PATH_INVALID = "workspace_path_invalid"
ALREADY_BOUND = "workspace_already_bound"
ARCHIVED = "workspace_archived"
LIMIT = "workspace_limit"
DIRTY = "workspace_dirty"
IN_USE = "workspace_in_use"
GIT_FAILED = "workspace_git_failed"
SOURCE_INVALID = "workspace_source_invalid"
# 工作区登记还在，但它的目录已经不在了（删了、移动了、盘符没挂上）
ROOT_MISSING = "workspace_root_missing"

__all__ = [
    "ALREADY_BOUND",
    "ARCHIVED",
    "DIRTY",
    "DISABLED",
    "GIT_DISABLED",
    "GIT_FAILED",
    "IN_USE",
    "LIMIT",
    "LOCAL_DISABLED",
    "NAME_TAKEN",
    "NOT_FOUND",
    "PATH_INVALID",
    "PATH_TAKEN",
    "ROOT_MISSING",
    "SOURCE_INVALID",
    "WorkspaceError",
]
