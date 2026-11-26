"""裁判校准记录与门禁分档。

「裁判分能不能拦发布」取决于是否有一份 n 足够、与人工打分相关性达标的校准记录。
但把门槛设成「全部指标 n≥100 且 ρ≥0.7」等于让裁判永远没有话语权：现实中没人会
先标 100 条再开始用，于是二值门禁只会把整批结论拖成 insufficient。

改为三档，让「有多少证据说多少话」：

| 档位 | 条件 | 裁判作用 |
|---|---|---|
| off  | 无记录 / 未达 soft | 只展示分数，verdict 记为 unvalidated（不判 fail） |
| soft | n≥30 且 ρ≥0.60 | 可把赛道从 pass 降为 unvalidated，仍不能判 fail |
| hard | n≥100 且 ρ≥0.70 | 可判 fail（真正的否决权） |

`valid` 保留为 `level == "hard"`，兼容旧调用方。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

MIN_CALIBRATION_N = 100
MIN_CALIBRATION_SPEARMAN = 0.7
SOFT_CALIBRATION_N = 30
SOFT_CALIBRATION_SPEARMAN = 0.6

LEVELS = ("off", "soft", "hard")

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


def _meets(row: dict[str, Any] | None, n: int, rho: float) -> bool:
    return row is not None and int(row["n"]) >= n and float(row["spearman"]) >= rho


def _level_of(values: list[dict[str, Any] | None]) -> str:
    """档位由**最差**一项决定：门禁要求全部指标达标，一项掉队就整档下调。"""
    if not values:
        return "off"
    if all(_meets(row, MIN_CALIBRATION_N, MIN_CALIBRATION_SPEARMAN) for row in values):
        return "hard"
    if all(_meets(row, SOFT_CALIBRATION_N, SOFT_CALIBRATION_SPEARMAN) for row in values):
        return "soft"
    return "off"


def _next_level(n: int | None, rho: float | None) -> str:
    """升到下一档还差什么；空串表示已到 hard。"""
    if n is None or rho is None:
        return "soft"
    if n >= MIN_CALIBRATION_N and rho >= MIN_CALIBRATION_SPEARMAN:
        return ""
    if n >= SOFT_CALIBRATION_N and rho >= SOFT_CALIBRATION_SPEARMAN:
        return "hard"
    return "soft"


def _progress(rows: list[dict[str, Any] | None]) -> dict[str, Any]:
    """校准进度：取各项指标里**最差**的一份，因为门禁要求全部指标达标。

    `n` / `spearman` 为 None 表示连记录都没有 —— 页面据此显示「先标 30 条」，
    而不是把 0 画成「已完成 0%」然后让人以为只差一点。
    """
    present = [row for row in rows if row is not None]
    n = min((int(row["n"]) for row in present), default=None)
    rho = min((float(row["spearman"]) for row in present), default=None)
    return {
        "n": n,
        "spearman": rho,
        "next": _next_level(n, rho),
        "soft": {"n": SOFT_CALIBRATION_N, "spearman": SOFT_CALIBRATION_SPEARMAN},
        "hard": {"n": MIN_CALIBRATION_N, "spearman": MIN_CALIBRATION_SPEARMAN},
    }


def _calibration_ids(values: list[dict[str, Any] | None]) -> list[int]:
    return sorted(
        int(row["id"])
        for row in values
        if row is not None and _meets(row, SOFT_CALIBRATION_N, SOFT_CALIBRATION_SPEARMAN)
    )


def calibration_gate(
    conn: sqlite3.Connection, *, metrics: list[str], judge_model: str, rubric_sha: str
) -> dict[str, Any]:
    """返回档位、缺口指标与进度；门禁要求**全部**指标达标才升档。"""
    rows = {
        metric: active_calibration(
            conn, metric=metric, judge_model=judge_model, rubric_sha=rubric_sha
        )
        for metric in metrics
    }
    values = list(rows.values())
    level = _level_of(values)
    progress = _progress(values)
    return {
        "checked": True,
        "valid": level == "hard",
        "level": level,
        "missing": [
            metric
            for metric, row in rows.items()
            if not _meets(row, MIN_CALIBRATION_N, MIN_CALIBRATION_SPEARMAN)
        ],
        "calibration_ids": _calibration_ids(values),
        "n": progress["n"],
        "spearman": progress["spearman"],
        "progress": progress,
    }
