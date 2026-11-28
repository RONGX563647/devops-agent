"""工作区存储 —— 把观测库的行映射成领域对象，事务边界在这一层。

SQL 全部在 `observability/repository/workspaces.py`；本模块只做「连接 + 领域对象」，
不写 SQL，也不做业务判定（白名单、限额、404/409 都在 service）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from devops_agent.observability import repository as repo
from devops_agent.workspace.models import Repository, Workspace, WorkspaceSession


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[Any]:
    """打开已建表的连接；一个 with 块就是一次原子操作。"""
    with repo.connect(db_path) as conn:
        repo.init_db(conn)
        yield conn


def _workspace(row: dict[str, Any]) -> Workspace:
    return Workspace(
        id=str(row["id"]),
        name=str(row["name"]),
        owner=str(row["owner"]),
        root_path=str(row["root_path"]),
        default_profile=str(row["default_profile"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        archived_at=row.get("archived_at"),
    )


def _repository(row: dict[str, Any]) -> Repository:
    return Repository(
        workspace_id=str(row["workspace_id"]),
        source=str(row["source"]),
        managed=bool(row["managed"]),
        remote_url=str(row["remote_url"]),
        branch=str(row["branch"]),
        base_commit=str(row["base_commit"]),
        credential_ref=str(row["credential_ref"]),
        detected_git=bool(row["detected_git"]),
        created_at=str(row["created_at"]),
    )


def _session(row: dict[str, Any]) -> WorkspaceSession:
    return WorkspaceSession(
        session_id=str(row["session_id"]),
        workspace_id=str(row["workspace_id"]),
        bound_at=str(row["bound_at"]),
        bound_by=str(row["bound_by"]),
    )


def list_workspaces(conn: Any, *, status: str | None = None) -> list[Workspace]:
    return [_workspace(row) for row in repo.list_workspaces(conn, status=status)]


def get_workspace(conn: Any, workspace_id: str) -> Workspace | None:
    row = repo.get_workspace(conn, workspace_id)
    return _workspace(row) if row is not None else None


def get_repository(conn: Any, workspace_id: str) -> Repository | None:
    row = repo.get_repository(conn, workspace_id)
    return _repository(row) if row is not None else None


def find_active_by_name(conn: Any, name: str) -> Workspace | None:
    row = repo.find_active_by_name(conn, name)
    return _workspace(row) if row is not None else None


def find_active_by_root(conn: Any, root_path: str) -> Workspace | None:
    row = repo.find_active_by_root(conn, root_path)
    return _workspace(row) if row is not None else None


def count_workspaces(conn: Any, *, status: str | None = None) -> int:
    return repo.count_workspaces(conn, status=status)


def insert(conn: Any, workspace: Workspace, repository: Repository) -> None:
    """同一事务写工作区与仓库来源。"""
    repo.insert_workspace(
        conn,
        {
            "id": workspace.id,
            "name": workspace.name,
            "owner": workspace.owner,
            "root_path": workspace.root_path,
            "default_profile": workspace.default_profile,
            "status": workspace.status,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
        },
        {
            "workspace_id": repository.workspace_id,
            "source": repository.source,
            "managed": repository.managed,
            "remote_url": repository.remote_url,
            "branch": repository.branch,
            "base_commit": repository.base_commit,
            "credential_ref": repository.credential_ref,
            "detected_git": repository.detected_git,
            "created_at": repository.created_at,
        },
    )


def update(conn: Any, workspace: Workspace, **fields: Any) -> Workspace:
    """按字段更新并返回更新后的领域对象（调用方负责先做唯一性校验）。"""
    repo.update_workspace(conn, workspace.id, **fields)
    row = repo.get_workspace(conn, workspace.id)
    return _workspace(row) if row is not None else replace(workspace, **fields)


def delete(conn: Any, workspace_id: str) -> bool:
    return repo.delete_workspace(conn, workspace_id)


def bind_session(conn: Any, session: WorkspaceSession) -> None:
    repo.bind_session(
        conn,
        session_id=session.session_id,
        workspace_id=session.workspace_id,
        bound_at=session.bound_at,
        bound_by=session.bound_by,
    )


def get_session_binding(conn: Any, session_id: str) -> WorkspaceSession | None:
    row = repo.get_session_binding(conn, session_id)
    return _session(row) if row is not None else None


def list_sessions(conn: Any, workspace_id: str) -> list[WorkspaceSession]:
    return [_session(row) for row in repo.list_workspace_sessions(conn, workspace_id)]


def count_sessions(conn: Any, workspace_id: str) -> int:
    return repo.count_workspace_sessions(conn, workspace_id)


def set_session_workspace(conn: Any, session_id: str, workspace_id: str) -> None:
    repo.set_session_workspace(conn, session_id, workspace_id)


def count_long_term(conn: Any, scope_id: str) -> int:
    return repo.count_long_term(conn, "workspace", scope_id)


def list_long_term(
    conn: Any, scope_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    return repo.list_long_term(
        conn, scope="workspace", scope_id=scope_id, limit=limit, offset=offset
    )


def delete_long_term(conn: Any, scope_id: str) -> int:
    return repo.delete_long_term_scope(conn, "workspace", scope_id)


__all__ = [
    "bind_session",
    "connect",
    "count_long_term",
    "count_sessions",
    "count_workspaces",
    "delete",
    "delete_long_term",
    "find_active_by_name",
    "find_active_by_root",
    "get_repository",
    "get_session_binding",
    "get_workspace",
    "insert",
    "list_long_term",
    "list_sessions",
    "list_workspaces",
    "set_session_workspace",
    "update",
]
