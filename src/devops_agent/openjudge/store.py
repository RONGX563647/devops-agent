"""OpenJudge 独立存储：批次 / 用例 / Rubric。

与生产库、旧 eval 库**完全分离**：默认落在 `workspace/evals/openjudge.db`
（已 gitignore）。CLI 写、API 只读；库缺失或不可写时读接口返回空，不抛 500。

为什么不复用 observability 的库：评测批次的生命周期（suite_sha / plan_sha /
rubric_sha 决定可比性）与生产 run 无关，混表会让「删旧批次」变成高风险操作。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from devops_agent.openjudge import compare as compare_lib
from devops_agent.openjudge import explain, grade, migrations
from devops_agent.openjudge.calibration import (
    MIN_CALIBRATION_N as MIN_CALIBRATION_N,
)
from devops_agent.openjudge.calibration import (
    MIN_CALIBRATION_SPEARMAN as MIN_CALIBRATION_SPEARMAN,
)
from devops_agent.openjudge.calibration import (
    active_calibration as active_calibration,
)
from devops_agent.openjudge.calibration import (
    calibration_gate as calibration_gate,
)
from devops_agent.openjudge.calibration import (
    insert_calibration as insert_calibration,
)
from devops_agent.openjudge.schema import SCHEMA

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = _REPO_ROOT / "workspace" / "evals" / "openjudge.db"


def now_iso() -> str:
    """UTC ISO 时间戳（秒级足够；批量任务不会在同一秒完成两次）。"""
    return datetime.now(UTC).isoformat()


def db_path() -> Path:
    override = os.environ.get("DEVOPS_AGENT_OPENJUDGE_DB_PATH", "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrations.migrate_case_columns(conn)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def insert_batch(
    conn: sqlite3.Connection,
    *,
    suite_sha: str,
    plan_sha: str,
    rubric_sha: str,
    prompt_sha: str,
    model: str,
    oj_version: str,
    config_id: str,
    started_at: str,
    finished_at: str,
    verdict: str,
    summary: dict[str, Any],
    refs_sha: str = "",
    calibration_id: int | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO oj_batches (
            suite_sha, refs_sha, plan_sha, rubric_sha, prompt_sha, calibration_id,
            model, oj_version, config_id, started_at, finished_at, status, verdict,
            summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)
        """,
        (
            suite_sha,
            refs_sha,
            plan_sha,
            rubric_sha,
            prompt_sha,
            calibration_id,
            model,
            oj_version,
            config_id,
            started_at,
            finished_at,
            verdict,
            json_dumps(summary),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def insert_case(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    result: dict[str, Any],
    trace: dict[str, Any],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO oj_cases (
            batch_id, case_id, track, risk, subject, status, passed, latency_ms, tokens,
            checks_json, judge_json, trace_json, events_json, input_stages_json, workspace_json,
            judge_norm, rule_passed, violations, trial, tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            result["case_id"],
            result.get("track", ""),
            result.get("risk", ""),
            result.get("subject", ""),
            result.get("status", ""),
            1 if result.get("passed") else 0,
            int(trace.get("latency_ms", 0) or 0),
            int((trace.get("tokens") or {}).get("input", 0))
            + int((trace.get("tokens") or {}).get("output", 0)),
            json_dumps(result.get("checks", [])),
            json_dumps(result.get("judge", {})),
            json_dumps(trace),
            json_dumps(trace.get("events", [])),
            json_dumps(trace.get("input_stages", [])),
            json_dumps(trace.get("workspace", {})),
            result.get("judge_norm"),
            1 if result.get("rule_passed") else 0,
            int(result.get("violations", 0) or 0),
            int(result.get("trial", trace.get("trial", 0)) or 0),
            str(result.get("tier", "must_pass") or "must_pass"),
        ),
    )
    return int(cursor.lastrowid or 0)


def insert_rubric(
    conn: sqlite3.Connection,
    *,
    name: str,
    version: str,
    sha: str,
    status: str,
    reviewer: str,
    meta: dict[str, Any] | None = None,
) -> int:
    payload = meta or {}
    cursor = conn.execute(
        """
        INSERT INTO oj_rubrics
            (name, version, sha, status, reviewer, reviewed_at, created_at, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            version,
            sha,
            status,
            reviewer,
            str(payload.get("reviewed_at", "")),
            datetime.now(UTC).isoformat(),
            json_dumps(payload),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _batch_filter(verdict: str, plan_sha: str) -> tuple[str, list[Any]]:
    """批次过滤条件：列表与计数必须用同一份，否则「共 N 批」和列表对不上。"""
    clauses: list[str] = []
    args: list[Any] = []
    if verdict:
        clauses.append("verdict = ?")
        args.append(verdict)
    if plan_sha:
        clauses.append("plan_sha = ?")
        args.append(plan_sha)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), args


def list_batches(
    conn: sqlite3.Connection,
    *,
    verdict: str = "",
    plan_sha: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, args = _batch_filter(verdict, plan_sha)
    rows = _rows(
        conn,
        f"SELECT * FROM oj_batches {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, max(1, min(limit, 200)), max(0, offset)),
    )
    for row in rows:
        row["summary"] = json.loads(row.pop("summary_json", "{}") or "{}")
    return rows


def count_batches(conn: sqlite3.Connection, *, verdict: str = "", plan_sha: str = "") -> int:
    """满足条件的真实批次数；分页控件据此算页数，不能用本页条数代替。"""
    where, args = _batch_filter(verdict, plan_sha)
    row = conn.execute(f"SELECT COUNT(*) FROM oj_batches {where}", tuple(args)).fetchone()
    return int(row[0]) if row else 0


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM oj_batches WHERE id = ?", (batch_id,))
    if not rows:
        return None
    row = rows[0]
    row["summary"] = json.loads(row.pop("summary_json", "{}") or "{}")
    return row


def list_cases(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    track: str = "",
    status: str = "",
    passed: bool | None = None,
    subject: str = "",
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    clauses = ["batch_id = ?"]
    args: list[Any] = [batch_id]
    if track:
        clauses.append("track = ?")
        args.append(track)
    if status:
        clauses.append("status = ?")
        args.append(status)
    if passed is not None:
        clauses.append("passed = ?")
        args.append(1 if passed else 0)
    if subject:
        clauses.append("subject = ?")
        args.append(subject)
    where = " AND ".join(clauses)
    total = int(
        conn.execute(f"SELECT COUNT(*) FROM oj_cases WHERE {where}", tuple(args)).fetchone()[0]
    )
    rows = _rows(
        conn,
        f"SELECT id, batch_id, case_id, track, risk, subject, status, passed, latency_ms, tokens, "
        f"tier, trial, judge_json FROM oj_cases WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
        (*args, max(1, min(limit, 500)), max(0, offset)),
    )
    for row in rows:
        row["passed"] = bool(row["passed"])
        row["judge"] = json.loads(row.pop("judge_json", "{}") or "{}")
    return rows, total


def get_case(conn: sqlite3.Connection, case_row_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM oj_cases WHERE id = ?", (case_row_id,))
    if not rows:
        return None
    row = rows[0]
    for key in (
        "checks_json",
        "judge_json",
        "trace_json",
        "events_json",
        "input_stages_json",
        "workspace_json",
    ):
        target = key.replace("_json", "")
        row[target] = json.loads(row.pop(key, "null") or "null") or (
            [] if target in ("checks", "events", "input_stages") else {}
        )
    row["passed"] = bool(row["passed"])
    explain.enrich_case_row(row, grade.load_plan())
    row["scoring"] = explain.case_scoring(row)
    return row


def rubrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT * FROM oj_rubrics ORDER BY id DESC LIMIT 100")
    for row in rows:
        row["meta"] = json.loads(row.pop("meta_json", "{}") or "{}")
    return rows


def compare(conn: sqlite3.Connection, base_id: int, head_id: int) -> dict[str, Any] | None:
    """两个批次的赛道 delta、MDE、可比性与翻转用例。"""
    base = get_batch(conn, base_id)
    head = get_batch(conn, head_id)
    if base is None or head is None:
        return None
    base_cases = {row["case_id"]: row["passed"] for row in _passed_rows(conn, base_id)}
    head_cases = {row["case_id"]: row["passed"] for row in _passed_rows(conn, head_id)}
    return compare_lib.compare_detail(base, head, base_cases, head_cases)


def _passed_rows(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT case_id, passed FROM oj_cases WHERE batch_id = ?", (batch_id,))


def health(conn: sqlite3.Connection) -> dict[str, Any]:
    """最近批次、违例与校准档位；校准这里真算一次，不再写死 None。

    原来 `calibration` 恒为 None，同一页面一边显示「校准有效」一边显示「未知」。
    """
    calibration = _health_calibration(conn)
    latest = _rows(conn, "SELECT * FROM oj_batches ORDER BY id DESC LIMIT 1")
    if not latest:
        return {
            "has_data": False,
            "latest_batch": None,
            "calibration": calibration,
            "anchors": None,
        }
    row = latest[0]
    summary = json.loads(row.pop("summary_json", "{}") or "{}")
    return {
        "has_data": True,
        "latest_batch": {
            "id": row["id"],
            "verdict": row["verdict"],
            "finished_at": row["finished_at"],
            "judge_n": summary.get("kpi", {}).get("judge_n", 0),
            "violations": summary.get("kpi", {}).get("violations", 0),
        },
        "calibration": calibration,
        "anchors": None,
        "flake": None,
    }


def _health_calibration(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """按当前判分方案与裁判模型算校准档位；算不出来就返回 None（显示「未知」）。

    `settings` 依赖本模块，所以只能函数内导入；模块级导入会成环。
    """
    from devops_agent.openjudge import settings as settings_lib

    try:
        plan = grade.load_plan()
        return calibration_gate(
            conn,
            metrics=list(plan.judges),
            judge_model=settings_lib.resolved()["judge_model"],
            rubric_sha=grade.rubric_sha(plan),
        )
    except (sqlite3.Error, OSError, KeyError, ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- 趋势与赛道


def series(conn: sqlite3.Connection, *, days: int = 30, limit: int = 200) -> list[dict[str, Any]]:
    """按批次升序的通过率序列，供质量页折线使用。"""
    cutoff = (datetime.now(UTC) - timedelta(days=max(1, days))).isoformat()
    rows = _rows(
        conn,
        """
        SELECT id, finished_at, verdict, summary_json FROM oj_batches
         WHERE finished_at <> '' AND finished_at >= ?
         ORDER BY id ASC LIMIT ?
        """,
        (cutoff, max(1, min(limit, 500))),
    )
    points: list[dict[str, Any]] = []
    for row in rows:
        summary = json.loads(row.get("summary_json", "{}") or "{}")
        kpi = summary.get("kpi", {})
        tracks = summary.get("tracks", {})
        points.append(
            {
                "batch_id": row["id"],
                "finished_at": row["finished_at"],
                "verdict": row["verdict"],
                "pass_rate": float(kpi.get("pass_rate", 0.0) or 0.0),
                "cases": int(kpi.get("cases", 0) or 0),
                "tracks": {key: float(val.get("pass_rate", 0.0)) for key, val in tracks.items()},
            }
        )
    return points


def latest_track_stats(
    conn: sqlite3.Connection, track: str, *, scan: int = 50
) -> dict[str, Any] | None:
    """最近一个包含该赛道的批次统计；没有就返回 None（前端显式显示「尚无批次」）。"""
    rows = _rows(
        conn,
        "SELECT id, finished_at, verdict, summary_json FROM oj_batches ORDER BY id DESC LIMIT ?",
        (max(1, scan),),
    )
    for row in rows:
        summary = json.loads(row.get("summary_json", "{}") or "{}")
        stats = (summary.get("tracks") or {}).get(track)
        if stats:
            return {
                "batch_id": row["id"],
                "finished_at": row["finished_at"],
                "pass_rate": float(stats.get("pass_rate", 0.0)),
                "verdict": str(stats.get("verdict", row["verdict"])),
            }
    return None
