"""裁判校准记录与门禁判定。

「裁判分能不能拦发布」取决于是否有一份 n 足够、与人工打分相关性达标的校准记录；
没有记录时判分照常展示，但 verdict 只能降级为 insufficient。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

MIN_CALIBRATION_N = 100
MIN_CALIBRATION_SPEARMAN = 0.7

SCHEMA = """
CREATE TABLE IF NOT EXISTS oj_calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric TEXT NOT NULL,
    judge_model TEXT NOT NULL,
    rubric_sha TEXT NOT NULL DEFAULT '',
    n INTEGER NOT NULL,
    spearman REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    reviewer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
"""


def insert_calibration(
    conn: sqlite3.Connection,
    *,
    metric: str,
    judge_model: str,
    rubric_sha: str,
    n: int,
    spearman: float,
    active: bool,
    reviewer: str,
    meta: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO oj_calibrations
            (metric, judge_model, rubric_sha, n, spearman, active, reviewer, created_at, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metric,
            judge_model,
            rubric_sha,
            int(n),
            float(spearman),
            int(bool(active)),
            reviewer,
            datetime.now(UTC).isoformat(),
            json.dumps(meta or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def active_calibration(
    conn: sqlite3.Connection, *, metric: str, judge_model: str, rubric_sha: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM oj_calibrations
        WHERE active = 1 AND metric = ? AND judge_model = ? AND rubric_sha = ?
        ORDER BY id DESC LIMIT 1
        """,
        (metric, judge_model, rubric_sha),
    ).fetchone()
    return dict(row) if row is not None else None


def calibration_gate(
    conn: sqlite3.Connection, *, metrics: list[str], judge_model: str, rubric_sha: str
) -> dict[str, Any]:
    missing: list[str] = []
    ids: set[int] = set()
    for metric in metrics:
        row = active_calibration(
            conn, metric=metric, judge_model=judge_model, rubric_sha=rubric_sha
        )
        if (
            row is None
            or int(row["n"]) < MIN_CALIBRATION_N
            or float(row["spearman"]) < MIN_CALIBRATION_SPEARMAN
        ):
            missing.append(metric)
        else:
            ids.add(int(row["id"]))
    return {
        "checked": True,
        "valid": not missing,
        "missing": missing,
        "calibration_ids": sorted(ids),
    }
