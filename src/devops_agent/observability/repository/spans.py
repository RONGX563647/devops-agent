"""步骤 span（spans）的读写与统计。"""

from __future__ import annotations

import sqlite3

from devops_agent.observability.models import SpanRecord

from .rows import _span_from_row


def add_span(conn: sqlite3.Connection, span: SpanRecord) -> int:
    """插入一个步骤 span，返回自增 id。"""
    cur = conn.execute(
        """INSERT INTO spans
           (run_id, type, name, start_offset_ms, duration_ms, input, output,
            tokens, status, error, input_tokens, output_tokens,
            cache_input_tokens, cache_creation_input_tokens, seq, parent_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            span.run_id,
            span.type,
            span.name,
            span.start_offset_ms,
            span.duration_ms,
            span.input,
            span.output,
            span.tokens,
            span.status,
            span.error,
            span.input_tokens,
            span.output_tokens,
            span.cache_input_tokens,
            span.cache_creation_input_tokens,
            span.seq,
            span.parent_id,
        ),
    )
    assert cur.lastrowid is not None  # INSERT 成功时 sqlite3 保证有 lastrowid
    return int(cur.lastrowid)


def list_spans(conn: sqlite3.Connection, run_id: int) -> list[SpanRecord]:
    """按 run 内序号列出一次运行的全部步骤。

    `seq` 是 recorder 落库时的单调序号（泳道与列表共用同一顺序）；老记录没有 seq
    （全为 0），退化为按 start_offset_ms 排，行为与旧版一致。
    """
    rows = conn.execute(
        "SELECT * FROM spans WHERE run_id=? ORDER BY seq, start_offset_ms, id", (run_id,)
    ).fetchall()
    return [_span_from_row(row) for row in rows]


def count_spans_by_type(conn: sqlite3.Connection, span_type: str) -> tuple[int, int]:
    """统计某类 span 的总数与失败数，返回 (总数, 失败数)。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS f "
        "FROM spans WHERE type=?",
        (span_type,),
    ).fetchone()
    return int(row["n"]), int(row["f"] or 0)


def span_stats(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """按 span 类型汇总：调用数、失败数、token 与缓存命中量。

    看板要回答「工具失败率」「权限拒绝率」「缓存命中率」，这些都能在 SQL 里一次算完，
    不必把所有 span 读进内存——生产库里 span 是最大的一张表。
    """
    rows = conn.execute(
        """SELECT type,
                  COUNT(*) AS calls,
                  SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                  COALESCE(SUM(tokens), 0) AS tokens,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_input_tokens), 0) AS cache_input_tokens
           FROM spans GROUP BY type"""
    ).fetchall()
    return {
        str(row["type"]): {
            "calls": int(row["calls"]),
            "errors": int(row["errors"] or 0),
            "tokens": int(row["tokens"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cache_input_tokens": int(row["cache_input_tokens"]),
        }
        for row in rows
    }
