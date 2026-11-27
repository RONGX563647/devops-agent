"""运行记录（runs）的读写、筛选与聚合。"""

from __future__ import annotations

import sqlite3

from devops_agent.observability.models import RunRecord

from .rows import _run_from_row


def create_run(conn: sqlite3.Connection, run: RunRecord) -> int:
    """插入一条运行记录，返回 id。

    幂等：若 request_id 非空且已存在，返回已有 id，不重复插入（防前端重试）。
    """
    if run.request_id:
        existing = conn.execute(
            "SELECT id FROM runs WHERE request_id=?", (run.request_id,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
    cur = conn.execute(
        """INSERT INTO runs
           (session_id, domain, status, input, output, started_at, ended_at,
            duration_ms, model, total_tokens, health_score,
            input_tokens, output_tokens, request_id, permission,
            cache_input_tokens, cache_creation_input_tokens, work_ms, wall_ms, model_params)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run.session_id,
            run.domain,
            run.status,
            run.input,
            run.output,
            run.started_at,
            run.ended_at,
            run.duration_ms,
            run.model,
            run.total_tokens,
            run.health_score,
            run.input_tokens,
            run.output_tokens,
            run.request_id or None,
            run.permission,
            run.cache_input_tokens,
            run.cache_creation_input_tokens,
            run.work_ms,
            run.wall_ms,
            run.model_params,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    output: str,
    ended_at: str,
    duration_ms: int,
    total_tokens: int,
    health_score: float | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    work_ms: int = 0,
    wall_ms: int = 0,
    error: str = "",
) -> None:
    """运行结束时回填结果与聚合指标（含 token 分拆、缓存命中与双口径耗时）。"""
    conn.execute(
        """UPDATE runs SET status=?, output=?, ended_at=?, duration_ms=?,
           total_tokens=?, health_score=?, input_tokens=?, output_tokens=?,
           cache_input_tokens=?, cache_creation_input_tokens=?, work_ms=?, wall_ms=?,
           error=?
           WHERE id=?""",
        (
            status,
            output,
            ended_at,
            duration_ms,
            total_tokens,
            health_score,
            input_tokens,
            output_tokens,
            cache_input_tokens,
            cache_creation_input_tokens,
            work_ms,
            wall_ms,
            error,
            run_id,
        ),
    )


def set_run_web_access(conn: sqlite3.Connection, run_id: int, payload: str) -> None:
    """写入本轮网络获取决策（JSON）。

    单独一条 UPDATE 而不是塞进 finish_run：决策在 finish 之前就确定了，
    而 finish 的 SQL 已经被多处断言钉住，改它只会扩大回归面。
    """
    conn.execute("UPDATE runs SET web_access=? WHERE id=?", (payload, run_id))


def get_run(conn: sqlite3.Connection, run_id: int) -> RunRecord | None:
    """按 id 取运行记录，不存在返回 None。"""
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_from_row(row) if row is not None else None


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[RunRecord]:
    """按开始时间倒序列出运行记录。"""
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def delete_run(conn: sqlite3.Connection, run_id: int) -> bool:
    """删除一条运行及其关联数据，返回是否命中。"""
    cur = conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
    return cur.rowcount > 0


def _run_filters(
    domain: str | None,
    status: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list[object]]:
    """构造 runs 筛选 WHERE 子句与参数（参数化，防注入）。"""
    clauses: list[str] = []
    params: list[object] = []
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    if status:
        clauses.append("status=?")
        params.append(status)
    if since:
        clauses.append("started_at>=?")
        params.append(since)
    if until:
        clauses.append("started_at<=?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_runs(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RunRecord]:
    """按条件筛选运行记录，倒序分页。"""
    where, params = _run_filters(domain, status, since, until)
    rows = conn.execute(
        "SELECT * FROM runs" + where + " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def count_runs(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """按条件统计运行记录数（配合 query_runs 做分页）。"""
    where, params = _run_filters(domain, status, since, until)
    row = conn.execute("SELECT COUNT(*) AS n FROM runs" + where, params).fetchone()
    return int(row["n"])


def summary_rows(conn: sqlite3.Connection) -> list[RunRecord]:
    """取出所有 run 的指标字段供聚合（不含 input/output 大文本）。"""
    rows = conn.execute(
        """SELECT id, session_id, domain, status, started_at, ended_at, duration_ms,
                  model, total_tokens, health_score, input_tokens, output_tokens, request_id,
                  cache_input_tokens, cache_creation_input_tokens, input, output, permission,
                  work_ms, wall_ms, error, model_params, web_access
           FROM runs"""
    ).fetchall()
    return [_run_from_row(row) for row in rows]
