"""评测批次与用例（eval_batches / eval_cases）的读写。

与 run 表分开：评测是离线批处理，不产生用户会话，混进 runs 会污染线上指标。
"""

from __future__ import annotations

import sqlite3

from .rows import _eval_batch_from_row, _eval_case_from_row


def create_eval_batch(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    suite: str,
    suite_version: str = "",
    suite_sha: str = "",
    prompt_id: str = "",
    prompt_sha: str = "",
    model: str = "",
    profile: str = "",
    status: str = "running",
    summary_json: str = "",
) -> int:
    """插入一个评测批次，返回 id。"""
    cur = conn.execute(
        """INSERT INTO eval_batches
           (suite, suite_version, suite_sha, prompt_id, prompt_sha, model, profile,
            started_at, finished_at, status, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (
            suite,
            suite_version,
            suite_sha,
            prompt_id,
            prompt_sha,
            model,
            profile,
            started_at,
            status,
            summary_json,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def insert_eval_case(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    case_id: str,
    status: str,
    passed: bool,
    run_id: int | None = None,
    scores_json: str = "",
    detail_json: str = "",
    latency_ms: int = 0,
    tokens: int = 0,
) -> int:
    """插入一条评测用例结果，返回 id。"""
    cur = conn.execute(
        """INSERT INTO eval_cases
           (batch_id, case_id, run_id, status, passed, scores_json, detail_json,
            latency_ms, tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            case_id,
            run_id,
            status,
            1 if passed else 0,
            scores_json,
            detail_json,
            latency_ms,
            tokens,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def finish_eval_batch(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    status: str,
    summary_json: str,
    finished_at: str,
) -> None:
    """回填批次的结束时间、状态与汇总。"""
    conn.execute(
        "UPDATE eval_batches SET status=?, summary_json=?, finished_at=? WHERE id=?",
        (status, summary_json, finished_at, batch_id),
    )


def list_eval_batches(
    conn: sqlite3.Connection, limit: int = 20, offset: int = 0
) -> list[dict[str, object]]:
    """按开始时间倒序列出评测批次。"""
    rows = conn.execute(
        "SELECT * FROM eval_batches ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_eval_batch_from_row(row) for row in rows]


def count_eval_batches(conn: sqlite3.Connection) -> int:
    """评测批次总数（分页配套）。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM eval_batches").fetchone()
    return int(row["n"])


def get_eval_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, object] | None:
    """按 id 取评测批次。"""
    row = conn.execute("SELECT * FROM eval_batches WHERE id=?", (batch_id,)).fetchone()
    return _eval_batch_from_row(row) if row is not None else None


def _like_escape(text: str) -> str:
    """转义 LIKE 元字符，让 tag 按字面匹配而不是当通配符。

    必须先转反斜杠：若先转 `%`/`_`，它们补出来的反斜杠会被这一步再转一次。
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _eval_case_filter(
    batch_id: int, status: str | None, tag: str | None
) -> tuple[str, list[object]]:
    """构造 eval_cases 的筛选条件（参数化）。"""
    clauses = ["batch_id=?"]
    params: list[object] = [batch_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if tag:
        # 标签存在 detail_json.tags 数组里，用带引号的精确匹配避免子串误命中；
        # tag 里的 % / _ 是 LIKE 通配符，不转义会让 a_b 命中 axb、% 命中全部。
        clauses.append("detail_json LIKE ? ESCAPE '\\'")
        params.append(f'%"{_like_escape(tag)}"%')
    return " WHERE " + " AND ".join(clauses), params


def list_eval_cases(
    conn: sqlite3.Connection,
    batch_id: int,
    status: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, object]]:
    """按批次列用例结果，可按状态 / 标签筛选。"""
    where, params = _eval_case_filter(batch_id, status, tag)
    rows = conn.execute(
        "SELECT * FROM eval_cases" + where + " ORDER BY id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_eval_case_from_row(row) for row in rows]


def count_eval_cases(
    conn: sqlite3.Connection,
    batch_id: int,
    status: str | None = None,
    tag: str | None = None,
) -> int:
    """筛选后的用例总数（分页配套）。"""
    where, params = _eval_case_filter(batch_id, status, tag)
    row = conn.execute("SELECT COUNT(*) AS n FROM eval_cases" + where, params).fetchone()
    return int(row["n"])
