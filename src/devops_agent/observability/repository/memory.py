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
    keys = row.keys()
    return {
        "id": int(row["id"]),
        "scope": str(row["scope"]),
        "scope_id": str(row["scope_id"]),
        "kind": str(row["kind"]),
        "text": str(row["text"]),
        "score": float(row["score"]),
        "hits": int(row["hits"] or 0) if "hits" in keys else 0,
        "last_hit_at": row["last_hit_at"] if "last_hit_at" in keys else None,
        "norm_text": row["norm_text"] if "norm_text" in keys else None,
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
    norm_text: str = "",
) -> dict[str, Any]:
    """按 `norm_text` 去重写入；返回 stored / deduped / id / hits。

    去重键从 `text` 换成归一化文本是 SPEC-0016 §8.4 的核心：旧实现比的是整段原文，
    两次不同措辞说同一件事会各存一行，条目只增不减，检索被同义条目稀释。

    `hits` 从 1 起算：它是「这条事实被强调过几次」而不是「被读过几次」——
    首次写入就算一次，之后每次同义命中、每次检索命中各加一（SPEC-0016 A33）。
    """
    normalized = norm_text or text
    existing = conn.execute(
        """SELECT id, hits, text FROM long_term_memories
           WHERE scope=? AND scope_id=?
             AND (norm_text=? OR (norm_text IS NULL AND text=?))""",
        (scope, scope_id, normalized, text),
    ).fetchone()
    if existing is not None:
        now = _now().isoformat()
        # 保留更长的正文：归一化相同说明是同一件事，信息量大的那份不该被短的盖掉
        keep = text if len(text) >= len(str(existing["text"])) else str(existing["text"])
        conn.execute(
            """UPDATE long_term_memories
               SET hits=hits+1, last_hit_at=?, text=?, norm_text=? WHERE id=?""",
            (now, keep, normalized, int(existing["id"])),
        )
        return {
            "stored": False,
            "deduped": True,
            "id": int(existing["id"]),
            "hits": int(existing["hits"] or 0) + 1,
        }
    created_at = _now().isoformat()
    cur = conn.execute(
        """INSERT INTO long_term_memories
           (scope, scope_id, kind, text, score, hits, last_hit_at, norm_text,
            created_at, expires_at)
           VALUES (?, ?, ?, ?, 0, 1, ?, ?, ?, ?)""",
        (scope, scope_id, kind, text, created_at, normalized, created_at, _expires_at(ttl_days)),
    )
    assert cur.lastrowid is not None
    new_id = int(cur.lastrowid)
    _prune_long_term(conn, scope, scope_id, max_entries)
    return {"stored": True, "deduped": False, "id": new_id, "hits": 1}


def merge_long_term(
    conn: sqlite3.Connection, memory_id: int, *, text: str, norm_text: str, kind: str
) -> dict[str, Any]:
    """同义条目合并：保留更长的正文、累加命中数并刷新最近使用时间。

    不新增行的理由是「一条事实应该只有一个条目」：两条 3-gram 相似度 0.85 的记忆
    同时在库里，注入时会被当成两条独立事实，权重翻倍且互相重复。
    """
    now = _now().isoformat()
    conn.execute(
        """UPDATE long_term_memories
           SET text=?, norm_text=?, kind=?, hits=hits+1, last_hit_at=?
           WHERE id=?""",
        (text, norm_text, kind, now, memory_id),
    )
    row = conn.execute(
        "SELECT id, hits FROM long_term_memories WHERE id=?", (memory_id,)
    ).fetchone()
    hits = int(row["hits"] or 0) if row is not None else 0
    return {"stored": False, "deduped": True, "merged": True, "id": memory_id, "hits": hits}


def touch_long_term(conn: sqlite3.Connection, memory_ids: list[int]) -> None:
    """检索命中即强化：`hits` 与 `last_hit_at` 决定淘汰顺序（越久未用越先淘汰）。"""
    if not memory_ids:
        return
    now = _now().isoformat()
    conn.executemany(
        "UPDATE long_term_memories SET hits=hits+1, last_hit_at=? WHERE id=?",
        [(now, int(memory_id)) for memory_id in memory_ids],
    )


def _prune_long_term(conn: sqlite3.Connection, scope: str, scope_id: str, max_entries: int) -> None:
    """按最近使用淘汰，而不是按创建时间：常用的旧条目不该被新噪声挤掉。"""
    conn.execute(
        """DELETE FROM long_term_memories WHERE scope=? AND scope_id=? AND id NOT IN (
               SELECT id FROM long_term_memories WHERE scope=? AND scope_id=?
               ORDER BY COALESCE(last_hit_at, created_at) DESC, id DESC LIMIT ?)""",
        (scope, scope_id, scope, scope_id, max(1, max_entries)),
    )


def list_long_term(
    conn: sqlite3.Connection,
    *,
    scope: str,
    scope_id: str,
    limit: int = 50,
    offset: int = 0,
    all_entries: bool = False,
    max_entries: int = 200,
) -> list[dict[str, Any]]:
    """列长期条目；`all_entries=True` 时取满 `max_entries`（检索用）。

    E-1 的根因就是这里：检索曾经传 `limit=max(top_k*20, 50)`，而 SQL 是
    `ORDER BY id DESC LIMIT`，第 51 条以后永远召回不到 —— `max_entries=200`
    里 75% 是死数据。检索必须全量取，分页接口才用 limit/offset。
    """
    cap = max(1, max_entries) if all_entries else max(1, limit)
    rows = conn.execute(
        """SELECT * FROM long_term_memories
           WHERE scope=? AND scope_id=? AND (expires_at IS NULL OR expires_at>?)
           ORDER BY id DESC LIMIT ? OFFSET ?""",
        (scope, scope_id, _now().isoformat(), cap, 0 if all_entries else max(0, offset)),
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


def delete_expired(conn: sqlite3.Connection) -> None:
    """清理两层过期条目；会话删除时由调用方级联。"""
    now = _now().isoformat()
    conn.execute(
        "DELETE FROM session_memories WHERE expires_at IS NOT NULL AND expires_at<=?", (now,)
    )
    conn.execute(
        "DELETE FROM long_term_memories WHERE expires_at IS NOT NULL AND expires_at<=?", (now,)
    )
