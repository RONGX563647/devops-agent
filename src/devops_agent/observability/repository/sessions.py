"""会话状态（session_states）快照的读写。"""

from __future__ import annotations

import sqlite3


def save_session_state(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    state_json: str,
    permission: str,
    updated_at: str,
) -> None:
    """整份覆盖写会话快照（AgentState 的 JSON）。"""
    conn.execute(
        """INSERT INTO session_states (session_id, state_json, permission, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             state_json=excluded.state_json,
             permission=excluded.permission,
             updated_at=excluded.updated_at""",
        (session_id, state_json, permission, updated_at),
    )


def load_session_state(conn: sqlite3.Connection, session_id: str) -> tuple[str, str] | None:
    """读取会话快照，返回 (state_json, permission)；没有则 None。"""
    row = conn.execute(
        "SELECT state_json, permission FROM session_states WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return str(row["state_json"] or ""), str(row["permission"] or "")


def delete_session_state(conn: sqlite3.Connection, session_id: str) -> bool:
    """删除会话快照，返回是否命中。"""
    cur = conn.execute("DELETE FROM session_states WHERE session_id=?", (session_id,))
    return cur.rowcount > 0
