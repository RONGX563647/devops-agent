"""挂起与恢复（权限确认等待）。

一条 run 在等用户确认时被标记 parked。所有状态迁移都用「带前置状态的条件
UPDATE」完成：影响 0 行即说明状态已被别人改过，调用方据此返回 409，
这样双击「允许」或两个标签页同时确认都不会重复执行。
"""

from __future__ import annotations

import sqlite3


def park_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    pending_json: str,
    expires_at: str,
) -> bool:
    """把 run 标记为等待确认；已处于挂起态时返回 False（幂等）。"""
    cur = conn.execute(
        "UPDATE runs SET parked=1, pending_json=?, expires_at=? WHERE id=? AND parked=0",
        (pending_json, expires_at, run_id),
    )
    return cur.rowcount > 0


def unpark_run(conn: sqlite3.Connection, run_id: int, *, now: str) -> bool:
    """解除挂起；已解除或已过期返回 False。

    过期判定放在 SQL 里一起做，避免「先查再改」的竞态窗口。
    """
    cur = conn.execute(
        """UPDATE runs SET parked=0, pending_json=NULL, expires_at=NULL
           WHERE id=? AND parked=1 AND (expires_at IS NULL OR expires_at > ?)""",
        (run_id, now),
    )
    return cur.rowcount > 0


def get_pending(conn: sqlite3.Connection, run_id: int) -> dict[str, object] | None:
    """取挂起信息：run_id / session_id / pending_json / expires_at / parked。"""
    row = conn.execute(
        """SELECT id, session_id, parked, pending_json, expires_at, permission
           FROM runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "parked": bool(row["parked"]),
        "pending_json": row["pending_json"],
        "expires_at": row["expires_at"],
        "permission": str(row["permission"] or ""),
    }


def active_run_for_session(conn: sqlite3.Connection, session_id: str) -> int | None:
    """同一会话是否已有未结束的 run（含挂起）。"""
    row = conn.execute(
        "SELECT id FROM runs WHERE session_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def bump_asks(conn: sqlite3.Connection, run_id: int) -> int:
    """把本 run 的确认次数 +1 并返回新值。"""
    conn.execute("UPDATE runs SET ask_count=ask_count+1 WHERE id=?", (run_id,))
    row = conn.execute("SELECT ask_count FROM runs WHERE id=?", (run_id,)).fetchone()
    return int(row["ask_count"]) if row is not None else 0
