"""工作区仓储（SPEC-0021）—— 三张表的唯一读写入口。

与其余仓储同一约束：只用 stdlib sqlite3，返回普通 dict，上层（`devops_agent.workspace`）
负责把它变成领域对象。反向依赖不存在：observability 不知道 workspace 包的存在。
"""

from __future__ import annotations

import sqlite3
from typing import Any

# 可被 PATCH 的字段白名单：列名来自这里，绝不拼接外部输入
_UPDATABLE = ("name", "default_profile", "status", "archived_at", "updated_at")


def _workspace_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "owner": str(row["owner"]),
        "root_path": str(row["root_path"]),
        "default_profile": str(row["default_profile"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "archived_at": row["archived_at"],
    }


def _repository_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "workspace_id": str(row["workspace_id"]),
        "source": str(row["source"]),
        "managed": bool(row["managed"]),
        "remote_url": str(row["remote_url"]),
        "branch": str(row["branch"]),
        "base_commit": str(row["base_commit"]),
        "credential_ref": str(row["credential_ref"]),
        "detected_git": bool(row["detected_git"]),
        "created_at": str(row["created_at"]),
    }


def insert_workspace(
    conn: sqlite3.Connection,
    workspace: dict[str, Any],
    repository: dict[str, Any],
) -> None:
    """在同一事务里写工作区与它的仓库来源（1:1，任何一半失败都整体回滚）。"""
    conn.execute(
        """INSERT INTO workspaces
           (id, name, owner, root_path, default_profile, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            workspace["id"],
            workspace["name"],
            workspace["owner"],
            workspace["root_path"],
            workspace["default_profile"],
            workspace["status"],
            workspace["created_at"],
            workspace["updated_at"],
        ),
    )
    conn.execute(
        """INSERT INTO workspace_repositories
           (workspace_id, source, managed, remote_url, branch, base_commit,
            credential_ref, detected_git, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            repository["workspace_id"],
            repository["source"],
            1 if repository["managed"] else 0,
            repository["remote_url"],
            repository["branch"],
            repository["base_commit"],
            repository["credential_ref"],
            1 if repository["detected_git"] else 0,
            repository["created_at"],
        ),
    )


def list_workspaces(conn: sqlite3.Connection, *, status: str | None = None) -> list[dict[str, Any]]:
    """按创建时间倒序列出工作区；`status` 省略表示全部。"""
    if status:
        rows = conn.execute(
            "SELECT * FROM workspaces WHERE status=? ORDER BY created_at DESC, id DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM workspaces ORDER BY created_at DESC, id DESC").fetchall()
    return [_workspace_row(row) for row in rows]


def get_workspace(conn: sqlite3.Connection, workspace_id: str) -> dict[str, Any] | None:
    """按 id 取工作区，不存在返回 None。"""
    row = conn.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    return _workspace_row(row) if row is not None else None


def get_repository(conn: sqlite3.Connection, workspace_id: str) -> dict[str, Any] | None:
    """取工作区的仓库来源（1:1），不存在返回 None。"""
    row = conn.execute(
        "SELECT * FROM workspace_repositories WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return _repository_row(row) if row is not None else None


def find_active_by_name(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """按名字查 active 工作区（唯一索引的软校验，给 409 用）。"""
    row = conn.execute(
        "SELECT * FROM workspaces WHERE name=? AND status='active'", (name,)
    ).fetchone()
    return _workspace_row(row) if row is not None else None


def find_active_by_root(conn: sqlite3.Connection, root_path: str) -> dict[str, Any] | None:
    """按 realpath 查 active 工作区：同一目录不能被两个工作区同时改写。"""
    row = conn.execute(
        "SELECT * FROM workspaces WHERE root_path=? AND status='active'", (root_path,)
    ).fetchone()
    return _workspace_row(row) if row is not None else None


def count_workspaces(conn: sqlite3.Connection, *, status: str | None = None) -> int:
    """统计工作区数；用于 `max_workspaces` 限额。"""
    if status:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM workspaces WHERE status=?", (status,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM workspaces").fetchone()
    return int(row["n"])


def update_workspace(conn: sqlite3.Connection, workspace_id: str, **fields: Any) -> None:
    """按白名单列更新工作区字段；空更新是 no-op。"""
    clean = {key: value for key, value in fields.items() if key in _UPDATABLE}
    if not clean:
        return
    assignments = ", ".join(f"{key}=?" for key in clean)
    conn.execute(
        f"UPDATE workspaces SET {assignments} WHERE id=?",  # noqa: S608 - 列名来自白名单
        (*clean.values(), workspace_id),
    )


def delete_workspace(conn: sqlite3.Connection, workspace_id: str) -> bool:
    """删除工作区登记；外键级联清理仓库来源与绑定。"""
    cur = conn.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
    return cur.rowcount > 0


def bind_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    workspace_id: str,
    bound_at: str,
    bound_by: str,
) -> None:
    """写入会话归属；同一会话只允许一条（PK），重复绑定由服务层先判 409。"""
    conn.execute(
        """INSERT INTO workspace_sessions (session_id, workspace_id, bound_at, bound_by)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             workspace_id=excluded.workspace_id,
             bound_at=excluded.bound_at,
             bound_by=excluded.bound_by""",
        (session_id, workspace_id, bound_at, bound_by),
    )


def get_session_binding(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """取会话的工作区归属，没有返回 None。"""
    row = conn.execute(
        "SELECT * FROM workspace_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "session_id": str(row["session_id"]),
        "workspace_id": str(row["workspace_id"]),
        "bound_at": str(row["bound_at"]),
        "bound_by": str(row["bound_by"]),
    }


def list_workspace_sessions(conn: sqlite3.Connection, workspace_id: str) -> list[dict[str, Any]]:
    """列出归属某工作区的会话。"""
    rows = conn.execute(
        "SELECT * FROM workspace_sessions WHERE workspace_id=? ORDER BY bound_at",
        (workspace_id,),
    ).fetchall()
    return [
        {
            "session_id": str(row["session_id"]),
            "workspace_id": str(row["workspace_id"]),
            "bound_at": str(row["bound_at"]),
            "bound_by": str(row["bound_by"]),
        }
        for row in rows
    ]


def count_workspace_sessions(conn: sqlite3.Connection, workspace_id: str) -> int:
    """统计归属某工作区的会话数。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_sessions WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    return int(row["n"])


# 左栏只显示一行标题，把整段输入搬进列表纯属浪费带宽：统一在 SQL 里截断。
_TITLE_CHARS = 80

# 「会话 + 运行聚合」是同一条 SQL 的两种口径（限定工作区 / 未归属），
# 只有 WHERE 与分组来源不同，所以共用这段 SELECT 与 ORDER BY。
_SESSION_STATS_SQL = """
    SELECT {source} AS session_id,
           {bound_at} AS bound_at,
           {bound_by} AS bound_by,
           COUNT(r.id) AS run_count,
           COALESCE(SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END), 0) AS failed_count,
           MAX(r.started_at) AS last_run_at,
           (SELECT substr(t.input, 1, {chars})
              FROM runs AS t
             WHERE t.session_id = {source}
             ORDER BY t.started_at ASC, t.id ASC
             LIMIT 1) AS title
      FROM {frm}
      {where}
     GROUP BY {source}
"""


def _session_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            "session_id": str(row["session_id"]),
            "bound_at": str(row["bound_at"] or ""),
            "bound_by": str(row["bound_by"] or ""),
            "title": str(row["title"] or ""),
            "run_count": int(row["run_count"] or 0),
            "failed_count": int(row["failed_count"] or 0),
            "last_run_at": str(row["last_run_at"] or ""),
        }
        for row in rows
    ]


def list_workspace_session_stats(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """某工作区下的会话及其运行聚合；标题取最早一条输入，时间取最近一次运行。

    绑定表是归属的唯一真相，所以从左表出发：刚绑定还没跑过的会话也要出现
    （`run_count=0`，排序回落到 `bound_at`）。
    """
    sql = (
        _SESSION_STATS_SQL.format(
            source="ws.session_id",
            bound_at="ws.bound_at",
            bound_by="ws.bound_by",
            chars=_TITLE_CHARS,
            frm="workspace_sessions ws LEFT JOIN runs r ON r.session_id = ws.session_id",
            where="WHERE ws.workspace_id = ?",
        )
        + " ORDER BY COALESCE(MAX(r.started_at), ws.bound_at) DESC, ws.session_id DESC"
        " LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, (workspace_id, limit, offset)).fetchall()
    return _session_rows(rows)


def list_unbound_session_stats(
    conn: sqlite3.Connection, *, limit: int = 50
) -> list[dict[str, Any]]:
    """还没归属任何工作区的会话（老会话保持可见，不因新模型上线而消失）。"""
    sql = (
        _SESSION_STATS_SQL.format(
            source="r.session_id",
            bound_at="NULL",
            bound_by="NULL",
            chars=_TITLE_CHARS,
            frm="runs r",
            where=(
                "WHERE NOT EXISTS (SELECT 1 FROM workspace_sessions ws"
                " WHERE ws.session_id = r.session_id)"
            ),
        )
        + " ORDER BY COALESCE(MAX(r.started_at), '') DESC, r.session_id DESC"
        " LIMIT ?"
    )
    rows = conn.execute(sql, (limit,)).fetchall()
    return _session_rows(rows)


def set_session_workspace(conn: sqlite3.Connection, session_id: str, workspace_id: str) -> None:
    """把绑定写进会话快照的审计列；快照不存在时不动（绑定表才是唯一真相）。"""
    conn.execute(
        "UPDATE session_states SET workspace_id=? WHERE session_id=?",
        (workspace_id, session_id),
    )


__all__ = [
    "bind_session",
    "count_workspace_sessions",
    "count_workspaces",
    "delete_workspace",
    "find_active_by_name",
    "find_active_by_root",
    "get_repository",
    "get_session_binding",
    "get_workspace",
    "insert_workspace",
    "list_unbound_session_stats",
    "list_workspace_session_stats",
    "list_workspace_sessions",
    "list_workspaces",
    "set_session_workspace",
    "update_workspace",
]
