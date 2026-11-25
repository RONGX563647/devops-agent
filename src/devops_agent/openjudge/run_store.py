"""`store` 的在线任务分片：`oj_runs` 表的生命周期读写。

从 `store.py` 拆出来只为一个原因 —— 单文件 500 行硬上限。放在一起时
`store.py` 会到 567 行，而 500 行是 `scripts/check_file_size.py` 明确拦的。

分片依据也很自然：`store.py` 存**评测结果**（批次/用例/rubric），本模块存
**任务状态**（谁在跑、跑到第几条、失败原因）。两者生命周期不同 —— 批次可以
永久留档，任务记录只是短期的进度载体，过几天就能删。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from devops_agent.openjudge.store import _rows, now_iso


def _now() -> str:
    """本地别名：调用点太多，保留短名字免得每行都写 `store.now_iso()`。"""
    return now_iso()


def claim_run(
    conn: sqlite3.Connection, *, track: str, total: int, judge: bool, concurrency: int
) -> int | None:
    """占用唯一的运行槽；已有任务在跑时返回 None（不抛异常，由调用方转 409）。

    单飞靠 `idx_oj_runs_active` 这个部分唯一索引保证，不是内存标志位 ——
    uvicorn 多 worker 时每个进程都有自己的内存，只有库能挡住第二个请求。
    """
    now = _now()
    try:
        cursor = conn.execute(
            """
            INSERT INTO oj_runs
                (status, track, total, done, judge, concurrency,
                 started_at, updated_at, finished_at)
            VALUES ('running', ?, ?, 0, ?, ?, ?, ?, '')
            """,
            (track, total, 1 if judge else 0, concurrency, now, now),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return None
    conn.commit()
    return int(cursor.lastrowid or 0)


def update_progress(conn: sqlite3.Connection, run_id: int, done: int) -> None:
    conn.execute(
        "UPDATE oj_runs SET done = ?, updated_at = ? WHERE id = ?",
        (max(0, done), _now(), run_id),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    batch_id: int | None = None,
    error: str = "",
) -> None:
    now = _now()
    conn.execute(
        """
        UPDATE oj_runs
           SET status = ?, batch_id = ?, error = ?, updated_at = ?, finished_at = ?,
               done = CASE WHEN ? = 'done' THEN total ELSE done END
         WHERE id = ?
        """,
        (status, batch_id, error, now, now, status, run_id),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM oj_runs WHERE id = ?", (run_id,))
    if not rows:
        return None
    return _run_out(rows[0])


def active_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    rows = _rows(
        conn,
        "SELECT * FROM oj_runs WHERE status IN ('pending', 'running') ORDER BY id DESC LIMIT 1",
    )
    return _run_out(rows[0]) if rows else None


def list_runs(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT * FROM oj_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),))
    return [_run_out(row) for row in rows]


def _run_out(row: dict[str, Any]) -> dict[str, Any]:
    # sqlite 没有 bool，judge 落成 0/1；对外统一成 bool，前端才不用判断类型。
    row["judge"] = bool(row.get("judge"))
    row["batch_id"] = int(row["batch_id"]) if row.get("batch_id") else None
    return row


def reap_stale_runs(conn: sqlite3.Connection, *, max_age_seconds: int = 600) -> int:
    """把重启后遗留的 pending/running 标成 failed，避免幽灵任务永久占住单飞槽。

    任务挂在进程内（`asyncio.create_task`），进程一死就再没人收尾。这里不做
    续跑 —— 续跑需要的断点信息比收益更贵；把残留标成 `interrupted` 让用户重跑
    是更诚实的处理。
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
    now = _now()
    cursor = conn.execute(
        """
        UPDATE oj_runs
           SET status = 'failed', error = 'interrupted', updated_at = ?, finished_at = ?
         WHERE status IN ('pending', 'running') AND updated_at < ?
        """,
        (now, now, cutoff),
    )
    conn.commit()
    return int(cursor.rowcount or 0)
