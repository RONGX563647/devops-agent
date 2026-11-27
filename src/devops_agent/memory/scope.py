"""长期记忆作用域解析 —— 绑定工作区与否决定 scope/scope_id（SPEC-0021 §7.1）。

单独成模块是为了能被无框架环境单测：`memory/runtime.py` 在 import 期会拉进
agentscope，而作用域规则本身只是纯函数。
"""

from __future__ import annotations

from typing import Any


def effective_scope(policy: Any, workspace_id: str | None) -> str:
    """生效作用域：绑定工作区时强制 `workspace`，否则沿用配置里的 scope。

    不允许「工作区查不到再回落 session」：那会把别的会话的记忆悄悄带进来。
    """
    if workspace_id:
        return "workspace"
    return str(policy.long_term.scope)


def resolve_scope_id(policy: Any, session_id: str, workspace_id: str | None) -> str:
    """把作用域落到存储键：workspace / user / session 三选一。"""
    scope = effective_scope(policy, workspace_id)
    if scope == "workspace":
        return f"workspace:{workspace_id}"
    if scope == "user" and policy.user_id:
        return str(policy.user_id)
    return f"session:{session_id}"


__all__ = ["effective_scope", "resolve_scope_id"]
