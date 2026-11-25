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

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = _REPO_ROOT / "workspace" / "evals" / "openjudge.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS oj_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_sha TEXT NOT NULL,
    refs_sha TEXT NOT NULL DEFAULT '',
    plan_sha TEXT NOT NULL DEFAULT '',
    rubric_sha TEXT NOT NULL DEFAULT '',
    prompt_sha TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    oj_version TEXT NOT NULL DEFAULT '',
    config_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    verdict TEXT NOT NULL DEFAULT 'insufficient',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS oj_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    case_id TEXT NOT NULL,
    track TEXT NOT NULL,
    risk TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    passed INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    checks_json TEXT NOT NULL DEFAULT '[]',
    judge_json TEXT NOT NULL DEFAULT '{}',
    trace_json TEXT NOT NULL DEFAULT '{}',
    events_json TEXT NOT NULL DEFAULT '[]',
    input_stages_json TEXT NOT NULL DEFAULT '[]',
    workspace_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_oj_cases_batch ON oj_cases(batch_id);
CREATE TABLE IF NOT EXISTS oj_rubrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    reviewer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS oj_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending',
    track TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    judge INTEGER NOT NULL DEFAULT 0,
    concurrency INTEGER NOT NULL DEFAULT 1,
    batch_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
-- 单飞：任何时刻只允许一个未完成任务。部分唯一索引在多进程下同样生效，
-- 内存标志位做不到这一点（uvicorn 多 worker 时会各放行一个）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_oj_runs_active
    ON oj_runs(status) WHERE status IN ('pending', 'running');
"""


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
    conn.commit()


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
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO oj_batches (
            suite_sha, refs_sha, plan_sha, rubric_sha, prompt_sha, model, oj_version,
            config_id, started_at, finished_at, status, verdict, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)
        """,
        (
            suite_sha,
            refs_sha,
            plan_sha,
            rubric_sha,
            prompt_sha,
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
            checks_json, judge_json, trace_json, events_json, input_stages_json, workspace_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    cursor = conn.execute(
        """
        INSERT INTO oj_rubrics (name, version, sha, status, reviewer, created_at, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            version,
            sha,
            status,
            reviewer,
            datetime.now(UTC).isoformat(),
            json_dumps(meta or {}),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def list_batches(
    conn: sqlite3.Connection,
    *,
    verdict: str = "",
    plan_sha: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if verdict:
        clauses.append("verdict = ?")
        args.append(verdict)
    if plan_sha:
        clauses.append("plan_sha = ?")
        args.append(plan_sha)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = _rows(
        conn,
        f"SELECT * FROM oj_batches {where} ORDER BY id DESC LIMIT ?",
        (*args, max(1, min(limit, 200))),
    )
    for row in rows:
        row["summary"] = json.loads(row.pop("summary_json", "{}") or "{}")
    return rows


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
        f"judge_json FROM oj_cases WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
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
    return row


def rubrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT * FROM oj_rubrics ORDER BY id DESC LIMIT 100")
    for row in rows:
        row["meta"] = json.loads(row.pop("meta_json", "{}") or "{}")
    return rows


def _compare_reasons(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key, label in (
        ("suite_sha", "题库"),
        ("plan_sha", "判分计划"),
        ("rubric_sha", "Rubric"),
        ("refs_sha", "参考答案"),
    ):
        if base.get(key) != head.get(key):
            reasons.append(f"{label}变化（{key}）")
    return reasons


def _track_deltas(base_tracks: dict[str, Any], head_tracks: dict[str, Any]) -> dict[str, Any]:
    tracks: dict[str, Any] = {}
    for track in sorted(set(base_tracks) | set(head_tracks)):
        old = base_tracks.get(track, {})
        new = head_tracks.get(track, {})
        tracks[track] = {
            "base_pass_rate": old.get("pass_rate"),
            "head_pass_rate": new.get("pass_rate"),
            "delta": (new.get("pass_rate", 0) - old.get("pass_rate", 0)) if old and new else None,
            "base_judge": old.get("judge_avg"),
            "head_judge": new.get("judge_avg"),
        }
    return tracks


def _case_counts(batch: dict[str, Any]) -> int:
    return int(batch["summary"].get("kpi", {}).get("cases", 0) or 0)


def _flips(base_cases: dict[str, Any], head_cases: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"case_id": case_id, "base": base_cases[case_id], "head": head_cases[case_id]}
        for case_id in sorted(set(base_cases) & set(head_cases))
        if base_cases[case_id] != head_cases[case_id]
    ]


def compare(conn: sqlite3.Connection, base_id: int, head_id: int) -> dict[str, Any] | None:
    """两个批次的赛道 delta、MDE、可比性与翻转用例。"""
    base = get_batch(conn, base_id)
    head = get_batch(conn, head_id)
    if base is None or head is None:
        return None
    reasons = _compare_reasons(base, head)
    smallest = min(_case_counts(base), _case_counts(head))
    base_cases = {row["case_id"]: row["passed"] for row in _passed_rows(conn, base_id)}
    head_cases = {row["case_id"]: row["passed"] for row in _passed_rows(conn, head_id)}
    return {
        "base": base_id,
        "head": head_id,
        "comparable": not reasons,
        "not_comparable_reasons": reasons,
        "mde": 1.0 / smallest if smallest else None,
        "tracks": _track_deltas(
            base["summary"].get("tracks", {}), head["summary"].get("tracks", {})
        ),
        "flips": _flips(base_cases, head_cases),
    }


def _passed_rows(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT case_id, passed FROM oj_cases WHERE batch_id = ?", (batch_id,))


def health(conn: sqlite3.Connection) -> dict[str, Any]:
    """最近批次、违例与干扰项概览；校准/锚点没有数据时显式返回 None。"""
    latest = _rows(conn, "SELECT * FROM oj_batches ORDER BY id DESC LIMIT 1")
    if not latest:
        return {"has_data": False, "latest_batch": None, "calibration": None, "anchors": None}
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
        "calibration": None,
        "anchors": None,
        "flake": None,
    }


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
