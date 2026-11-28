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
            cache_input_tokens, cache_creation_input_tokens, seq, parent_id, ttft_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            span.ttft_ms,
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


def model_usage(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """按模型聚合用量与耗时：每个模型的 token、调用数、平均耗时与平均首 token 延迟。

    成本必须按模型分别算（单价不同），所以这里不能只给一个全局合计；
    `ttft_samples` 单独给出「有几条记录真的有 TTFT」，没记录不等于 0 毫秒。
    """
    rows = conn.execute(
        """SELECT name,
                  COUNT(*) AS calls,
                  SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_input_tokens), 0) AS cache_input_tokens,
                  SUM(CASE WHEN duration_ms IS NOT NULL THEN 1 ELSE 0 END) AS timed,
                  COALESCE(SUM(duration_ms), 0) AS duration_ms,
                  SUM(CASE WHEN ttft_ms > 0 THEN 1 ELSE 0 END) AS ttft_samples,
                  COALESCE(SUM(CASE WHEN ttft_ms > 0 THEN ttft_ms ELSE 0 END), 0) AS ttft_sum
           FROM spans
           WHERE type='model'
           GROUP BY name
           ORDER BY calls DESC, name"""
    ).fetchall()
    return [
        {
            "name": str(row["name"]),
            "calls": int(row["calls"]),
            "errors": int(row["errors"] or 0),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cache_input_tokens": int(row["cache_input_tokens"]),
            "avg_ms": (
                round(int(row["duration_ms"]) / int(row["timed"])) if int(row["timed"]) else None
            ),
            "ttft_samples": int(row["ttft_samples"] or 0),
            "avg_ttft_ms": (
                round(int(row["ttft_sum"]) / int(row["ttft_samples"]))
                if int(row["ttft_samples"] or 0)
                else None
            ),
        }
        for row in rows
    ]


def timing_samples(conn: sqlite3.Connection, span_type: str, limit: int = 500) -> list[int]:
    """某类 span 的耗时样本（最近 N 条），用于算均值与分位。

    SQLite 没有 percentile 函数，分位只能取样本在内存里算；用「最近 N 条」
    限制内存，同时保证看板反映的是当前表现而不是半年前的历史。
    """
    rows = conn.execute(
        "SELECT duration_ms FROM spans "
        "WHERE type=? AND duration_ms IS NOT NULL ORDER BY id DESC LIMIT ?",
        (span_type, limit),
    ).fetchall()
    return [int(row["duration_ms"]) for row in rows]


def ttft_samples(conn: sqlite3.Connection, limit: int = 500) -> list[int]:
    """首 token 延迟样本（只取模型 span 且真的记录过 TTFT 的行）。"""
    rows = conn.execute(
        "SELECT ttft_ms FROM spans "
        "WHERE type='model' AND ttft_ms > 0 ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [int(row["ttft_ms"]) for row in rows]


def error_spans(conn: sqlite3.Connection, limit: int = 500) -> list[dict[str, str]]:
    """失败的 span（工具/模型/权限）：类型、名字与原因原文，供失败原因聚类。

    这是「失败原因记录不全」的补口：以前只聚 run.error，工具报错只落在
    span.status='error'，原因字段根本没人写，所以看板上一片「未记录失败原因」。
    """
    rows = conn.execute(
        """SELECT type, name, COALESCE(error, '') AS error
           FROM spans WHERE status='error' ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {"type": str(row["type"]), "name": str(row["name"]), "error": str(row["error"] or "")}
        for row in rows
    ]
