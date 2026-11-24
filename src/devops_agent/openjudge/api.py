"""OpenJudge 评测只读 API —— 批次、用例、对比、覆盖与 rubric。

与 `observability/eval_api.py` 同一套约定：
- 只读，写入只由 CLI 完成；
- 数据库不可用（文件缺失/表缺失）时返回空列表或 404，不放大成 500；
- 字段与 CLI 产出的 summary_json 对齐，前端不用二次解析。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Query

from devops_agent.openjudge import benchmark, store

router = APIRouter(prefix="/api/openjudge", tags=["openjudge"])

T = TypeVar("T")

# openjudge/api.py -> openjudge -> devops_agent -> src -> 仓库根
_CASES_DIR = Path(__file__).resolve().parents[3] / "eval" / "openjudge" / "cases"


def _read(operation: Callable[[sqlite3.Connection], T]) -> T | None:
    """短连接只读；数据库不可用时返回 None 而不是抛 500。"""
    try:
        with store.connect() as conn:
            store.init_db(conn)
            return operation(conn)
    except (sqlite3.Error, OSError):
        return None


@router.get("/health")
def openjudge_health() -> dict[str, object]:
    """最新批次概览；没有数据时显式 has_data=false。"""
    payload = _read(store.health)
    if payload is None:
        return {"has_data": False, "latest_batch": None, "calibration": None, "anchors": None}
    return payload


@router.get("/batches")
def openjudge_batches(
    verdict: str = Query("", max_length=24),
    plan_sha: str = Query("", max_length=80),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    rows = _read(
        lambda conn: store.list_batches(conn, verdict=verdict, plan_sha=plan_sha, limit=limit)
    )
    items = rows or []
    return {"items": items, "total": len(items)}


@router.get("/batches/{batch_id}")
def openjudge_batch(batch_id: int) -> dict[str, object]:
    row = _read(lambda conn: store.get_batch(conn, batch_id))
    if row is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return row


@router.get("/batches/{batch_id}/cases")
def openjudge_cases(
    batch_id: int,
    track: str = Query("", max_length=2),
    status: str = Query("", max_length=24),
    subject: str = Query("", max_length=48),
    passed: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    payload = _read(
        lambda conn: store.list_cases(
            conn,
            batch_id,
            track=track,
            status=status,
            subject=subject,
            passed=passed,
            limit=limit,
            offset=offset,
        )
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="批次不存在或数据库不可用")
    rows, total = payload
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/cases/{case_row_id}")
def openjudge_case(case_row_id: int) -> dict[str, object]:
    row = _read(lambda conn: store.get_case(conn, case_row_id))
    if row is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    return row


@router.get("/compare")
def openjudge_compare(
    base: int = Query(..., ge=1),
    head: int = Query(..., ge=1),
) -> dict[str, object]:
    payload = _read(lambda conn: store.compare(conn, base, head))
    if payload is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return payload


@router.get("/rubrics")
def openjudge_rubrics() -> dict[str, object]:
    rows = _read(store.rubrics)
    items = rows or []
    return {"items": items, "total": len(items)}


@router.get("/coverage")
def openjudge_coverage() -> dict[str, object]:
    """题库覆盖（赛道/风险/主题/工具）与校验问题，前端「覆盖」面板直接用。"""
    paths = sorted(_CASES_DIR.glob("*.jsonl"))
    if not paths:
        return {"cases": 0, "suite_sha": "", "coverage": {}, "problems": []}
    cases = benchmark.load_cases(paths)
    problems = benchmark.validate(cases, _CASES_DIR.parent / "fixtures", strict=False)
    return {
        "cases": len(cases),
        "suite_sha": benchmark.suite_sha(cases),
        "coverage": benchmark.coverage(cases),
        "problems": problems,
    }
