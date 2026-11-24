"""质量评分（eval_scores）的读写。"""

from __future__ import annotations

import sqlite3

from devops_agent.observability.models import EvalScoreRecord

from .rows import _score_from_row


def add_score(conn: sqlite3.Connection, score: EvalScoreRecord) -> int:
    """插入一条质量评分，返回自增 id。"""
    cur = conn.execute(
        """INSERT INTO eval_scores
           (run_id, dimension, score, source, rater, created_at, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            score.run_id,
            score.dimension,
            score.score,
            score.source,
            score.rater,
            score.created_at,
            score.comment,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def list_scores(conn: sqlite3.Connection, run_id: int) -> list[EvalScoreRecord]:
    """列出一次运行的全部评分。"""
    rows = conn.execute(
        "SELECT * FROM eval_scores WHERE run_id=? ORDER BY created_at, id", (run_id,)
    ).fetchall()
    return [_score_from_row(row) for row in rows]


def all_scores(conn: sqlite3.Connection) -> list[EvalScoreRecord]:
    """取出全部评分供趋势聚合。"""
    rows = conn.execute("SELECT * FROM eval_scores ORDER BY created_at, id").fetchall()
    return [_score_from_row(row) for row in rows]
