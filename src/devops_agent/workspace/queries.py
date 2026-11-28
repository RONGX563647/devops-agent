"""工作区的只读投影 —— 详情 / 列表 / 能力自述的纯函数。

单独成文件是为了让 `service.py` 留在 500 行棘轮以内；这里不碰数据库，
也不知道生命周期，只把领域对象摊平成 HTTP/CLI 共用的 dict。
"""

from __future__ import annotations

from typing import Any

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.workspace.models import Repository, Workspace


def scope_id(workspace_id: str) -> str:
    """长期记忆的工作区作用域键（与 MemoryRuntime 的解析一致）。"""
    return f"workspace:{workspace_id}"


def summary(workspace: Workspace, repository: Repository | None) -> dict[str, Any]:
    """工作区 + 仓库来源的扁平视图；前端要显示 source / managed / branch。"""
    return {
        "id": workspace.id,
        "name": workspace.name,
        "owner": workspace.owner,
        "root_path": workspace.root_path,
        "default_profile": workspace.default_profile,
        "status": workspace.status,
        "created_at": workspace.created_at,
        "updated_at": workspace.updated_at,
        "archived_at": workspace.archived_at,
        "source": repository.source if repository else "",
        "managed": repository.managed if repository else False,
        "remote_url": repository.remote_url if repository else "",
        "branch": repository.branch if repository else "",
        "base_commit": repository.base_commit if repository else "",
        "detected_git": repository.detected_git if repository else False,
    }


def session_item(row: dict[str, Any]) -> dict[str, Any]:
    """一条「工作区下的会话」：标题留原文截断，前端再按宽度压缩。

    `last_at` 取最近一次运行；刚绑定还没跑过的会话回落到绑定时间，
    否则排序会把它们永远排在最后，用户看不到自己刚挂上去的会话。
    """
    return {
        "session_id": str(row.get("session_id") or ""),
        "title": str(row.get("title") or ""),
        "last_at": str(row.get("last_run_at") or row.get("bound_at") or ""),
        "run_count": int(row.get("run_count") or 0),
        "failed_count": int(row.get("failed_count") or 0),
        "bound_at": str(row.get("bound_at") or ""),
        "bound_by": str(row.get("bound_by") or ""),
        # 空串表示未归档：省掉前端把 null / "" 分两种情况处理
        "archived_at": str(row.get("archived_at") or ""),
    }


def control(policy: WorkspacePolicy, *, git_available: bool) -> dict[str, Any]:
    """能力自述：开关、白名单、git 是否可用（/health 与前端共用）。"""
    return {
        "enabled": policy.enabled,
        "allow_local_import": policy.allow_local_import,
        "allow_git_import": policy.allow_git_import,
        "allowed_local_roots": list(policy.allowed_local_roots),
        "git_available": git_available,
        "max_workspaces": policy.max_workspaces,
        "default_profile": policy.default_profile,
    }


__all__ = ["control", "scope_id", "session_item", "summary"]
