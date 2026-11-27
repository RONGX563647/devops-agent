"""工作区请求依赖 —— 只依赖 config 与 workspace，不拉进 runtime/agentscope。

单独成模块的原因：工作区路由应能在无框架环境里单测，而 `deps.py` 为了会话注册表
必须 import runtime（进而 import agentscope）。这里保持叶子，`deps.py` 再转出。
"""

from __future__ import annotations

from fastapi import Request

from devops_agent.config import load_workspace
from devops_agent.gateway.errors import workspace_http
from devops_agent.workspace import WorkspaceError, WorkspaceRef, WorkspaceService


def workspace_service_of(request: Request) -> WorkspaceService:
    """取工作区服务：`app.state.workspace_service` 可注入（测试 / 部署自定义）。"""
    found = getattr(request.app.state, "workspace_service", None)
    if isinstance(found, WorkspaceService):
        return found
    return WorkspaceService(load_workspace())


def resolve_workspace(
    request: Request, session_id: str, requested_id: str | None
) -> WorkspaceRef | None:
    """解析本轮执行目录：开关关或未绑定都回落到旧路径（SPEC-0021 §6.1）。"""
    service = workspace_service_of(request)
    if not service.policy.enabled:
        return None
    try:
        return service.resolve(session_id, requested_id)
    except WorkspaceError as exc:
        raise workspace_http(exc.code) from None


__all__ = ["resolve_workspace", "workspace_service_of"]
