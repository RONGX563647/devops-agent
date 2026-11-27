"""记忆仓储 —— 中期版本表与长期条目的唯一读写入口。

与 observability 其余仓储同一约束：只用 stdlib sqlite3，上层不写 SQL。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def _expires_at(days: int) -> str:
    return (_now() + timedelta(days=days)).isoformat()


def _loads(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "version": int(row["version"]),
        "is_active": bool(row["is_active"]),
        "trigger": str(row["trigger"]),
        "text": str(row["text"]),
        "artifacts": _loads(row["artifacts"]),
        "pending": _loads(row["pending"]),
        "errors": _loads(row["errors"]),
        "tokens_before": int(row["tokens_before"]),
        "tokens_after": int(row["tokens_after"]),
        "budget": int(row["budget"]),
        "source_run_ids": [int(item) for item in _loads(row["source_run_ids"])],
        "created_at": str(row["created_at"]),
        "expires_at": row["expires_at"],
    }


def save_memory_version(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    version: int,
    trigger: str,
    text: str,
    artifacts: list[str],
    pending: list[str],
    errors: list[str],
    tokens_before: int,
    tokens_after: int,
    budget: int,
    source_run_ids: list[int],
    ttl_days: int,
    max_versions: int,
) -> dict[str, Any]:
    """写入一条中期版本并原子切换 active；返回落库后的记录。

    1) 新版本先以 is_active=0 插入；
    2) 旧 active 置 0，新版本置 1（同一事务）；
    3) 超出 max_versions 的最旧版本删除。
    """
    created_at = _now().isoformat()
    expires = _expires_at(ttl_days)
    cur = conn.execute(
        """INSERT INTO session_memories
           (session_id, version, is_active, trigger, text, artifacts, pending, errors,
            tokens_before, tokens_after, budget, source_run_ids, created_at, expires_at)
           VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            version,
            trigger,
            text,
            json.dumps(artifacts, ensure_ascii=False),
            json.dumps(pending, ensure_ascii=False),
            json.dumps(errors, ensure_ascii=False),
            tokens_before,
            tokens_after,
            budget,
            json.dumps(source_run_ids),
            created_at,
            expires,
        ),
    )
    assert cur.lastrowid is not None
    new_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE session_memories SET is_active=0 WHERE session_id=? AND id<>?",
        (session_id, new_id),
    )
    conn.execute("UPDATE session_memories SET is_active=1 WHERE id=?", (new_id,))
    conn.execute(
        """DELETE FROM session_memories WHERE session_id=? AND id NOT IN (
               SELECT id FROM session_memories WHERE session_id=?
               ORDER BY version DESC, id DESC LIMIT ?)""",
        (session_id, session_id, max(1, max_versions)),
    )
    row = conn.execute("SELECT * FROM session_memories WHERE id=?", (new_id,)).fetchone()
    return _memory_row(row)


def active_memory(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """当前 active 版本；过期或不存在返回 None。"""
    row = conn.execute(
        """SELECT * FROM session_memories
           WHERE session_id=? AND is_active=1 AND (expires_at IS NULL OR expires_at>?)""",
        (session_id, _now().isoformat()),
    ).fetchone()
    return _memory_row(row) if row is not None else None


def latest_version(conn: sqlite3.Connection, session_id: str) -> int:
    """该会话已有的最大版本号；无记录为 0。"""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM session_memories WHERE session_id=?", (session_id,)
    ).fetchone()
    return int(row["v"] or 0)


def list_memory_versions(
    conn: sqlite3.Connection, session_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM session_memories WHERE session_id=?
           ORDER BY version DESC, id DESC LIMIT ?""",
        (session_id, max(1, limit)),
    ).fetchall()
    return [_memory_row(row) for row in rows]


def delete_memory_version(conn: sqlite3.Connection, memory_id: int) -> bool:
    cur = conn.execute("DELETE FROM session_memories WHERE id=?", (memory_id,))
    return cur.rowcount > 0


def delete_session_memories(conn: sqlite3.Connection, session_id: str) -> int:
    cur = conn.execute("DELETE FROM session_memories WHERE session_id=?", (session_id,))
    return int(cur.rowcount)


# --- 长期 -----------------------------------------------------------------


def _long_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "scope": str(row["scope"]),
        "scope_id": str(row["scope_id"]),
        "kind": str(row["kind"]),
        "text": str(row["text"]),
        "score": float(row["score"]),
        "created_at": str(row["created_at"]),
        "expires_at": row["expires_at"],
    }


def save_long_term(
    conn: sqlite3.Connection,
    *,
    scope: str,
    scope_id: str,
    kind: str,
    text: str,
    ttl_days: int,
    max_entries: int,
) -> dict[str, Any]:
    """按 (scope, scope_id, text) 去重写入；返回 stored / deduped / id。"""
    existing = conn.execute(
        "SELECT id FROM long_term_memories WHERE scope=? AND scope_id=? AND text=?",
        (scope, scope_id, text),
    ).fetchone()
    if existing is not None:
        return {"stored": False, "deduped": True, "id": int(existing["id"])}
    created_at = _now().isoformat()
    cur = conn.execute(
        """INSERT INTO long_term_memories
           (scope, scope_id, kind, text, score, created_at, expires_at)
           VALUES (?, ?, ?, ?, 0, ?, ?)""",
        (scope, scope_id, kind, text, created_at, _expires_at(ttl_days)),
    )
    assert cur.lastrowid is not None
    new_id = int(cur.lastrowid)
    conn.execute(
        """DELETE FROM long_term_memories WHERE scope=? AND scope_id=? AND id NOT IN (
               SELECT id FROM long_term_memories WHERE scope=? AND scope_id=?
               ORDER BY created_at DESC, id DESC LIMIT ?)""",
        (scope, scope_id, scope, scope_id, max(1, max_entries)),
    )
    return {"stored": True, "deduped": False, "id": new_id}


def list_long_term(
    conn: sqlite3.Connection, *, scope: str, scope_id: str, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM long_term_memories
           WHERE scope=? AND scope_id=? AND (expires_at IS NULL OR expires_at>?)
           ORDER BY id DESC LIMIT ? OFFSET ?""",
        (scope, scope_id, _now().isoformat(), max(1, limit), max(0, offset)),
    ).fetchall()
    return [_long_row(row) for row in rows]


def count_long_term(conn: sqlite3.Connection, scope: str, scope_id: str) -> int:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM long_term_memories
           WHERE scope=? AND scope_id=? AND (expires_at IS NULL OR expires_at>?)""",
        (scope, scope_id, _now().isoformat()),
    ).fetchone()
    return int(row["n"])


def delete_long_term(conn: sqlite3.Connection, memory_id: int) -> bool:
    cur = conn.execute("DELETE FROM long_term_memories WHERE id=?", (memory_id,))
    return cur.rowcount > 0


def delete_long_term_scope(conn: sqlite3.Connection, scope: str, scope_id: str) -> int:
    """删除某个作用域下的全部长期条目，返回删除条数。

    删除工作区时默认清理 `scope='workspace'` 的条目（SPEC-0021 §8）；
    归档保留，因此只有显式删除会调用这里。
    """
    cur = conn.execute(
        "DELETE FROM long_term_memories WHERE scope=? AND scope_id=?", (scope, scope_id)
    )
    return cur.rowcount


def delete_expired(conn: sqlite3.Connection) -> None:
    """清理两层过期条目；会话删除时由调用方级联。"""
    now = _now().isoformat()
    conn.execute(
        "DELETE FROM session_memories WHERE expires_at IS NOT NULL AND expires_at<=?", (now,)
    )
    conn.execute(
        "DELETE FROM long_term_memories WHERE expires_at IS NOT NULL AND expires_at<=?", (now,)
    )
