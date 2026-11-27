"""记忆评测落库：`oj_memory_*` 专用表（SPEC §11.1）。

与 openjudge 共用同一个 SQLite 文件；读接口在库不可用或表缺失时返回空/None，
不把「还没跑过」放大成 500。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from devops_agent.openjudge import store as oj_store
from devops_agent.openjudge.memory.harness import CaseRun, ProbeResult
from devops_agent.openjudge.memory.schema import SCHEMA


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw) if raw else fallback
    except json.JSONDecodeError:
        return fallback


def insert_batch(
    conn: sqlite3.Connection,
    *,
    plan_name: str,
    suite_sha: str,
    plan_sha: str,
    config_sha: str,
    model: str,
    oj_version: str,
    started_at: str,
    finished_at: str,
    verdict: str,
    summary: dict[str, Any],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO oj_memory_batches (
            plan_name, suite_sha, plan_sha, config_sha, model, oj_version,
            started_at, finished_at, status, verdict, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)
        """,
        (
            plan_name,
            suite_sha,
            plan_sha,
            config_sha,
            model,
            oj_version,
            started_at,
            finished_at,
            verdict,
            _dumps(summary),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def insert_variant(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    name: str,
    overrides: dict[str, Any],
    config_sha: str,
) -> None:
    conn.execute(
        "INSERT INTO oj_memory_variants (batch_id, name, overrides_json, config_sha) "
        "VALUES (?, ?, ?, ?)",
        (batch_id, name, _dumps(overrides), config_sha),
    )


def insert_case(conn: sqlite3.Connection, batch_id: int, run: CaseRun) -> int:
    cursor = conn.execute(
        """
        INSERT INTO oj_memory_cases (
            batch_id, case_id, track, tier, variant, trial, passed, rule_passed,
            probe_total, probe_correct, latency_ms, tokens, probe_json, metrics_json,
            memory_events_json, trace_json, run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            run.case_id,
            run.track,
            run.tier,
            run.variant,
            run.trial,
            1 if run.passed else 0,
            1 if run.rule_passed else 0,
            len(run.probes),
            sum(1 for probe in run.probes if probe.correct),
            int(run.cost.get("latency_ms", 0) or 0),
            int(run.cost.get("input_tokens", 0) or 0),
            _dumps([probe.as_dict() for probe in run.probes]),
            _dumps(run.metrics),
            _dumps(run.memory_events),
            _dumps(run.trace),
            run.run_id,
            oj_store.now_iso(),
        ),
    )
    return int(cursor.lastrowid or 0)


def insert_probe(conn: sqlite3.Connection, case_row_id: int, probe: ProbeResult) -> None:
    conn.execute(
        """
        INSERT INTO oj_memory_probes
            (case_row_id, question, expected_json, answer, correct, judge_json, created_at)
        VALUES (?, ?, ?, ?, ?, '{}', ?)
        """,
        (
            case_row_id,
            probe.question,
            _dumps(probe.expected),
            probe.answer,
            1 if probe.correct else 0,
            oj_store.now_iso(),
        ),
    )


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def list_batches(
    conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT * FROM oj_memory_batches ORDER BY id DESC LIMIT ? OFFSET ?",
        (max(1, min(limit, 200)), max(0, offset)),
    )
    for row in rows:
        row["summary"] = _loads(row.pop("summary_json", "{}"), {})
    return rows


def count_batches(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM oj_memory_batches").fetchone()
    return int(row[0]) if row else 0


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM oj_memory_batches WHERE id = ?", (batch_id,))
    if not rows:
        return None
    row = rows[0]
    row["summary"] = _loads(row.pop("summary_json", "{}"), {})
    row["variants"] = _rows(
        conn,
        "SELECT name, overrides_json, config_sha FROM oj_memory_variants WHERE batch_id = ?",
        (batch_id,),
    )
    for variant in row["variants"]:
        variant["overrides"] = _loads(variant.pop("overrides_json", "{}"), {})
    return row


def list_cases(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    variant: str = "",
    track: str = "",
    passed: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    clauses = ["batch_id = ?"]
    args: list[Any] = [batch_id]
    if variant:
        clauses.append("variant = ?")
        args.append(variant)
    if track:
        clauses.append("track = ?")
        args.append(track)
    if passed is not None:
        clauses.append("passed = ?")
        args.append(1 if passed else 0)
    where = " AND ".join(clauses)
    total = int(
        conn.execute(f"SELECT COUNT(*) FROM oj_memory_cases WHERE {where}", tuple(args)).fetchone()[
            0
        ]
    )
    rows = _rows(
        conn,
        f"SELECT id, batch_id, case_id, track, tier, variant, trial, passed, rule_passed, "
        f"probe_total, probe_correct, latency_ms, tokens, metrics_json "
        f"FROM oj_memory_cases WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
        (*args, max(1, min(limit, 500)), max(0, offset)),
    )
    for row in rows:
        row["passed"] = bool(row["passed"])
        row["rule_passed"] = bool(row["rule_passed"])
        row["metrics"] = _loads(row.pop("metrics_json", "{}"), {})
    return rows, total


def get_case(conn: sqlite3.Connection, case_row_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM oj_memory_cases WHERE id = ?", (case_row_id,))
    if not rows:
        return None
    row = rows[0]
    row["passed"] = bool(row["passed"])
    row["rule_passed"] = bool(row["rule_passed"])
    row["probes"] = _loads(row.pop("probe_json", "[]"), [])
    row["metrics"] = _loads(row.pop("metrics_json", "{}"), {})
    row["memory_events"] = _loads(row.pop("memory_events_json", "[]"), [])
    row["trace"] = _loads(row.pop("trace_json", "{}"), {})
    return row


def list_probes(conn: sqlite3.Connection, case_row_id: int) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT * FROM oj_memory_probes WHERE case_row_id = ? ORDER BY id",
        (case_row_id,),
    )
    for row in rows:
        row["correct"] = bool(row["correct"])
        row["expected"] = _loads(row.pop("expected_json", "[]"), [])
        row["judge"] = _loads(row.pop("judge_json", "{}"), {})
    return rows


def probes_for_run(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    row = conn.execute(
        "SELECT id FROM oj_memory_cases WHERE run_id = ? ORDER BY id LIMIT 1", (run_id,)
    ).fetchone()
    if row is None:
        return []
    return list_probes(conn, int(row["id"]))


def _track_map(batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return batch["summary"].get("tracks") or {}


def compare(conn: sqlite3.Connection, base_id: int, head_id: int) -> dict[str, Any] | None:
    """两次批次的逐赛道 delta（TSR 与探针正确率）。"""
    base = get_batch(conn, base_id)
    head = get_batch(conn, head_id)
    if base is None or head is None:
        return None
    base_tracks = _track_map(base)
    head_tracks = _track_map(head)
    deltas: dict[str, dict[str, Any]] = {}
    for track in sorted(set(base_tracks) | set(head_tracks)):
        before = base_tracks.get(track) or {}
        after = head_tracks.get(track) or {}
        before_tsr = before.get("tsr")
        after_tsr = after.get("tsr")
        deltas[track] = {
            "base_tsr": before_tsr,
            "head_tsr": after_tsr,
            "tsr_delta": (
                round(float(after_tsr) - float(before_tsr), 4)
                if before_tsr is not None and after_tsr is not None
                else None
            ),
            "probe_acc_delta": _delta(before.get("probe_acc"), after.get("probe_acc")),
        }
    return {
        "base": {"id": base["id"], "verdict": base["verdict"], "finished_at": base["finished_at"]},
        "head": {"id": head["id"], "verdict": head["verdict"], "finished_at": head["finished_at"]},
        "deltas": deltas,
    }


def _delta(before: Any, after: Any) -> float | None:
    if before is None or after is None:
        return None
    return round(float(after) - float(before), 4)
