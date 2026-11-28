"""长期记忆作用域解析 —— 绑定工作区与否决定 scope/scope_id（SPEC-0021 §7.1）。

单独成模块是为了能被无框架环境单测：`memory/runtime.py` 在 import 期会拉进
agentscope，而作用域规则本身只是纯函数。

运行时、管理接口、上下文工具必须共用这一条规则。管理接口曾经把 `session` 与
`user` 两个分支写得一模一样（都返回 `policy.user_id or ...`），`scope=session`
时读到的其实是用户分区，看到的条目数与召回结果对不上。
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


def resolve_scope_id(
    policy: Any,
    session_id: str,
    workspace_id: str | None = None,
    *,
    scope: str | None = None,
) -> str:
    """把作用域落到存储键：workspace / user / session 三选一。

    `scope` 只在管理接口按查询参数切换时显式传；绑定工作区的会话以
    `workspace_id` 为准，`scope` 不再参与判定。
    """
    effective = "workspace" if workspace_id else str(scope or policy.long_term.scope)
    if effective == "workspace":
        return f"workspace:{workspace_id}"
    if effective == "user" and policy.user_id:
        return str(policy.user_id)
    return f"session:{session_id}"


__all__ = ["effective_scope", "resolve_scope_id"]
