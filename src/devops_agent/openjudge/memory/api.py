"""记忆评测 API（SPEC §11.2）。

读接口只读 `oj_memory_*`；写接口只有「触发一次确定性批次」。
`POST /runs` 同步执行（确定性套件几秒内完成），不再引入后台任务与单飞锁的复杂度。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from devops_agent.openjudge import store as oj_store
from devops_agent.openjudge.memory import runner, store

router = APIRouter(prefix="/api/quality/memory", tags=["memory"])

T = TypeVar("T")


def _read(operation: Callable[[sqlite3.Connection], T]) -> T | None:
    try:
        with oj_store.connect() as conn:
            oj_store.init_db(conn)
            store.init_db(conn)
            return operation(conn)
    except (sqlite3.Error, OSError):
        return None


@router.get("/batches")
def memory_batches(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    payload = _read(
        lambda conn: (
            store.list_batches(conn, limit=limit, offset=offset),
            store.count_batches(conn),
        )
    )
    items, total = payload or ([], 0)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/batches/{batch_id}")
def memory_batch(batch_id: int) -> dict[str, object]:
    row = _read(lambda conn: store.get_batch(conn, batch_id))
    if row is None:
        raise HTTPException(status_code=404, detail="记忆批次不存在")
    return row


@router.get("/batches/{batch_id}/cases")
def memory_cases(
    batch_id: int,
    variant: str = Query("", max_length=32),
    track: str = Query("", max_length=8),
    passed: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    payload = _read(
        lambda conn: store.list_cases(
            conn,
            batch_id,
            variant=variant,
            track=track,
            passed=passed,
            limit=limit,
            offset=offset,
        )
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="批次不存在或数据库不可用")
    rows, total = payload
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/compare")
def memory_compare(
    base: int = Query(..., ge=1),
    head: int = Query(..., ge=1),
) -> dict[str, object]:
    payload = _read(lambda conn: store.compare(conn, base, head))
    if payload is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return payload


@router.get("/cases/{case_row_id}")
def memory_case(case_row_id: int) -> dict[str, object]:
    """用例明细：注入内容、检索命中、探针答案都在这里。"""
    row = _read(lambda conn: store.get_case(conn, case_row_id))
    if row is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    probes = _read(lambda conn: store.list_probes(conn, case_row_id)) or []
    row["online_probes"] = probes
    return row


@router.get("/runs/{run_id}/memory-probe")
def memory_probe_for_run(run_id: str) -> dict[str, object]:
    """单次运行的在线探针结果；没有对应用例时返回空列表而不是 404。"""
    probes = _read(lambda conn: store.probes_for_run(conn, run_id)) or []
    return {"run_id": run_id, "probes": probes}


class MemoryRunIn(BaseModel):
    plan: str = ""
    suite: str = Field(default="must_pass", max_length=32)
    variants: list[str] = Field(default_factory=list)


_running = False


@router.post("/runs", status_code=202)
async def memory_start_run(body: MemoryRunIn) -> dict[str, object]:
    """触发一次确定性批次；已有批次在跑时返回 409。"""
    global _running
    if _running:
        raise HTTPException(status_code=409, detail="已有记忆评测在运行")
    plan_path = Path(body.plan) if body.plan else runner.DEFAULT_PLAN
    _running = True
    try:
        result = await asyncio.to_thread(_run_sync, plan_path, body)
    except runner.BatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        _running = False
    return {
        "batch_id": result.batch_id,
        "verdict": result.verdict,
        "summary": result.summary,
    }


def _run_sync(plan_path: Path, body: MemoryRunIn) -> runner.BatchResult:
    return asyncio.run(
        runner.run_plan(
            plan_path,
            suite_tier=body.suite,
            variants=body.variants or None,
        )
    )
