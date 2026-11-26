"""OpenJudge API —— 批次/用例/对比/覆盖只读，外加「在线发起评测」的四个写端点。

约定：
- 读接口在库不可用（文件缺失/表缺失）时返回空列表或 404，不放大成 500；
- 写接口只有「起任务 / 取消」两个动作，真正落库的仍是 `run.execute` 同一条流水线；
- 字段与 CLI 产出的 summary_json 对齐，前端不用二次解析。
"""

from __future__ import annotations

import copy
import sqlite3
from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from devops_agent.openjudge import benchmark, casestore, explain, grade, report, service, store

router = APIRouter(prefix="/api/openjudge", tags=["openjudge"])

T = TypeVar("T")


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


@router.get("/batches/{batch_id}/cases/{case_row_id}")
def openjudge_case(batch_id: int, case_row_id: int) -> dict[str, object]:
    """批次内某一条用例明细。

    挂在批次下而不是顶层的 `/cases/{id}`：顶层那个路径要留给**题库**（可编辑的
    用例定义），两者 id 空间完全不同，混在同一层级迟早返回错对象。
    """
    row = _read(lambda conn: store.get_case(conn, case_row_id))
    if row is None or int(row.get("batch_id", 0)) != batch_id:
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
    """题库覆盖（赛道/风险/主题/工具）与校验问题。

    口径是**仓库题库**：这是 CI 覆盖率基线的同一份事实。本地覆盖层改了哪几条由
    `overlay` / `deleted` 两个计数单独说明，避免「我改了两条 → 覆盖率下跌」的误读。
    """
    cases = casestore.repo_cases()
    if not cases:
        return {"cases": 0, "suite_sha": "", "coverage": {}, "problems": [], "overlay": 0}
    return {
        "cases": len(cases),
        "suite_sha": benchmark.suite_sha(cases),
        "coverage": benchmark.coverage(cases),
        "problems": benchmark.validate(cases, casestore.FIXTURES, strict=False),
        **casestore.suite_view(),
    }


@router.get("/suites")
def openjudge_suites() -> dict[str, object]:
    """赛道清单：用例数、阈值、最近一批通过率。

    阈值从 `report.TRACK_RULES` 下发，前端不再自己维护一份「门禁线」——
    之前那套写死在前端，取的是 `items[0]`，结果拿到的是最旧的套件。
    """
    cases = casestore.load_cases()
    coverage = benchmark.coverage(cases) if cases else {"tracks": {}}
    latest = _read(_latest_tracks) or {}
    return {
        "total": len(cases),
        "suite_sha": benchmark.suite_sha(cases) if cases else "",
        "tracks": [
            {
                "track": track,
                "cases": int(coverage.get("tracks", {}).get(track, 0)),
                "rules": report.TRACK_RULES.get(track, {}),
                "latest": latest.get(track),
            }
            for track in benchmark.TRACKS
        ],
    }


def _latest_tracks(conn: sqlite3.Connection) -> dict[str, dict[str, object] | None]:
    return {track: store.latest_track_stats(conn, track) for track in benchmark.TRACKS}


@router.get("/scoring-config")
def openjudge_scoring_config() -> dict[str, object]:
    """评分契约的唯一来源：计划/门槛/赛道规则都来自执行它们的代码。"""
    plan = grade.load_plan()
    return {
        "plan": {
            "name": plan.name,
            "sha": grade.plan_sha(plan),
            "judges": [
                {
                    "key": spec.key,
                    "grader": spec.grader,
                    "weight": spec.weight,
                    "scale": [spec.low, spec.high],
                }
                for spec in plan.judges.values()
            ],
        },
        "pass_judge": explain.PASS_JUDGE,
        "track_rules": copy.deepcopy(report.TRACK_RULES),
    }


@router.get("/series")
def openjudge_series(days: int = Query(30, ge=1, le=365)) -> dict[str, object]:
    points = _read(lambda conn: store.series(conn, days=days))
    return {"points": points or []}


class RunStartIn(BaseModel):
    track: str = ""
    case_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=service.DEFAULT_LIMIT, ge=1, le=service.MAX_LIMIT)
    judge: bool = False
    concurrency: int = Field(default=2, ge=1, le=service.MAX_CONCURRENCY)


@router.post("/runs", status_code=202)
async def openjudge_start_run(body: RunStartIn) -> dict[str, object]:
    """在线发起一次评测。track 与 case_ids 二选一；默认不启用裁判。"""
    if body.case_ids and body.track.strip():
        raise HTTPException(status_code=400, detail="track 与 case_ids 二选一，不能同时提供")
    try:
        payload = await service.start(
            track=body.track.strip().upper(),
            limit=body.limit,
            judge=body.judge,
            concurrency=body.concurrency,
            case_ids=body.case_ids,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "busy" in payload:
        raise HTTPException(
            status_code=409,
            detail={"message": "已有评测在运行", "run_id": payload.get("busy")},
        )
    return payload


@router.get("/runs")
def openjudge_runs(limit: int = Query(10, ge=1, le=50)) -> dict[str, object]:
    items = service.recent(limit)
    return {"items": items, "total": len(items)}


@router.get("/runs/{run_id}")
def openjudge_run(run_id: int) -> dict[str, object]:
    row = service.status(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row


@router.post("/runs/{run_id}/cancel", status_code=202)
def openjudge_cancel_run(run_id: int) -> dict[str, object]:
    row = service.cancel(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"id": row["id"], "status": row["status"]}
