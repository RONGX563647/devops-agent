"""输入解析阶段（input_stages）的读写。"""

from __future__ import annotations

import sqlite3

from devops_agent.observability.models import InputStageRecord

from .rows import _input_stage_from_row


def add_input_stages(conn: sqlite3.Connection, run_id: int, stages: list[InputStageRecord]) -> int:
    """批量写入一次运行的全部解析阶段，返回写入条数。

    全量落库（包括 skip 的「无改动」阶段）：界面只展示有用的那几条，
    但审计与复现需要完整的处理链，否则无法回答「当时到底做了什么」。
    """
    if not stages:
        return 0
    conn.executemany(
        """INSERT INTO input_stages
           (run_id, seq, stage, label, status, changed, summary,
            before_chars, after_chars, items, duration_ms, detail, failed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                run_id,
                stage.seq,
                stage.stage,
                stage.label,
                stage.status,
                1 if stage.changed else 0,
                stage.summary,
                stage.before,
                stage.after,
                stage.items,
                stage.duration_ms,
                stage.detail,
                stage.failed,
            )
            for stage in stages
        ],
    )
    return len(stages)


def list_input_stages(conn: sqlite3.Connection, run_id: int) -> list[InputStageRecord]:
    """按执行顺序列出一条运行的解析阶段。"""
    rows = conn.execute(
        "SELECT * FROM input_stages WHERE run_id=? ORDER BY seq, id", (run_id,)
    ).fetchall()
    return [_input_stage_from_row(row) for row in rows]
