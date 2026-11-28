"""会话归属与「按工作区查会话」（SPEC-0021 A6 / 左栏分组）。

`bind` / `resolve` 是归属的写入面：一条会话只允许一个工作区，改投必须 409；
`for_workspace` / `board` 是读取面：工作区底下挂最近会话，外带「未归属」一栏。

独立成模块而不是继续堆在 `service.py`：那边已到 500 行棘轮上限，且这组用例
自成一个职责簇（归属判定 + 检索投影），与创建/删除/记忆的耦合只有 policy 与库路径。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from devops_agent.workspace import errors, queries, store
from devops_agent.workspace.models import WorkspaceRef, WorkspaceSession, now_iso

if TYPE_CHECKING:  # 只为类型标注；运行期不 import，避免与 service 循环依赖
    from devops_agent.workspace.service import WorkspaceService


def bind(service: WorkspaceService, session_id: str, workspace_id: str) -> WorkspaceSession:
    """显式绑定；已绑到别处一律 409，不做「以最后一次为准」。"""
    session = str(session_id or "").strip()
    if not session:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, "session_id 不能为空")
    with store.connect(service.db_path) as conn:
        workspace = store.get_workspace(conn, workspace_id)
        if workspace is None:
            raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
        if workspace.status != "active":
            raise errors.WorkspaceError(errors.ARCHIVED, "工作区已归档，不能绑定")
        existing = store.get_session_binding(conn, session)
        if existing is not None:
            if existing.workspace_id != workspace_id:
                raise errors.WorkspaceError(
                    errors.ALREADY_BOUND,
                    "会话已绑定其他工作区",
                    workspace_id=existing.workspace_id,
                )
            return existing
        binding = WorkspaceSession(
            session_id=session,
            workspace_id=workspace_id,
            bound_at=now_iso(),
            bound_by="local",
        )
        store.bind_session(conn, binding)
        store.set_session_workspace(conn, session, workspace_id)
    return binding


def resolve(
    service: WorkspaceService, session_id: str, requested_id: str | None = None
) -> WorkspaceRef | None:
    """一轮请求的执行目录解析：显式 > 已绑定 > 旧路径（None）。"""
    if not service.policy.enabled:
        return None
    requested = str(requested_id or "").strip() or None
    with store.connect(service.db_path) as conn:
        binding = store.get_session_binding(conn, session_id)
        if requested is not None:
            return _bind_and_ref(conn, session_id, requested, binding)
        return _bound_ref(conn, binding)


def _bind_and_ref(
    conn: Any,
    session_id: str,
    requested: str,
    binding: WorkspaceSession | None,
) -> WorkspaceRef:
    """显式 id：校验 active 与归属一致；首次出现即落绑定。"""
    workspace = store.get_workspace(conn, requested)
    if workspace is None:
        raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
    if workspace.status != "active":
        raise errors.WorkspaceError(errors.ARCHIVED, "工作区已归档")
    if binding is not None and binding.workspace_id != requested:
        raise errors.WorkspaceError(
            errors.ALREADY_BOUND,
            "会话已绑定其他工作区，需新开会话",
            workspace_id=binding.workspace_id,
        )
    if binding is None:
        store.bind_session(
            conn,
            WorkspaceSession(
                session_id=session_id,
                workspace_id=requested,
                bound_at=now_iso(),
                bound_by="local",
            ),
        )
        store.set_session_workspace(conn, session_id, requested)
    return WorkspaceRef(id=workspace.id, root_path=workspace.root_path)


def _bound_ref(conn: Any, binding: WorkspaceSession | None) -> WorkspaceRef | None:
    """已绑定：工作区被删则回落旧路径；归档则拒绝。"""
    if binding is None:
        return None
    workspace = store.get_workspace(conn, binding.workspace_id)
    if workspace is None:
        return None  # 工作区已删除：回到旧路径，由调用方提示
    if workspace.status != "active":
        raise errors.WorkspaceError(errors.ARCHIVED, "工作区已归档")
    return WorkspaceRef(id=workspace.id, root_path=workspace.root_path)


def for_workspace(
    service: WorkspaceService, workspace_id: str, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """某工作区下的会话（最近运行优先），分页口径与长期记忆列表一致。"""
    with store.connect(service.db_path) as conn:
        workspace = store.get_workspace(conn, workspace_id)
        if workspace is None:
            raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
        rows = store.session_stats(conn, workspace_id, limit=limit, offset=offset)
        total = store.count_sessions(conn, workspace_id)
    return {
        "workspace_id": workspace_id,
        "items": [queries.session_item(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def board(service: WorkspaceService, *, limit_per_workspace: int = 5) -> dict[str, Any]:
    """左栏的分组视图：每个工作区挂最近几条会话，外加「未归属」一栏。

    未归属会话来自 runs 而非绑定表——老会话没有归属行，若只查绑定表，
    升级后它们会整批从侧栏消失，用户会以为数据丢了。
    """
    with store.connect(service.db_path) as conn:
        groups: list[dict[str, Any]] = []
        for workspace in store.list_workspaces(conn):
            repository = store.get_repository(conn, workspace.id)
            payload = queries.summary(workspace, repository)
            payload["session_count"] = store.count_sessions(conn, workspace.id)
            rows = store.session_stats(conn, workspace.id, limit=limit_per_workspace)
            payload["sessions"] = [queries.session_item(row) for row in rows]
            groups.append(payload)
        unbound = [
            queries.session_item(row)
            for row in store.unbound_session_stats(conn, limit=limit_per_workspace)
        ]
    return {"items": _ordered(groups), "unbound": unbound}


def _ordered(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """活跃在前，各自按「最近有动静」倒序；没有会话就看工作区自己的更新时间。"""
    return sorted(
        [item for item in groups if item["status"] == "active"],
        key=_activity,
        reverse=True,
    ) + sorted(
        [item for item in groups if item["status"] != "active"],
        key=_activity,
        reverse=True,
    )


def _activity(group: dict[str, Any]) -> str:
    stamps = [item["last_at"] for item in group["sessions"] if item.get("last_at")]
    return max(stamps) if stamps else str(group.get("updated_at") or "")


__all__ = ["bind", "board", "for_workspace", "resolve"]
