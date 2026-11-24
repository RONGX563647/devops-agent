"""挂起态查询与确认续跑：只做归属/状态/过期校验与续跑装配。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from devops_agent.gateway.deps import _profile_or_403, _recorder_for, _registry
from devops_agent.gateway.schemas import ConfirmRequest
from devops_agent.gateway.sse import _SSE_HEADERS, _sse_frame
from devops_agent.gateway.stream import SessionLease, _finish_or_park
from devops_agent.governance.profiles import (
    PermissionProfile,
    UnknownProfile,
    default_profile,
    resolve,
)
from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import utc_now
from devops_agent.runtime.confirm import parse_decisions

router = APIRouter()


def _pending_row(run_id: int) -> dict[str, object]:
    """读挂起记录并做归属/状态/过期三项校验。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        row = repo.get_pending(conn, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return row


def _check_pending(row: dict[str, object], session_id: str) -> None:
    if row["session_id"] != session_id:
        raise HTTPException(status_code=403, detail="session_mismatch")
    if not row["parked"]:
        raise HTTPException(status_code=409, detail="already_resolved")
    expires = str(row["expires_at"] or "")
    if expires and expires <= utc_now():
        raise HTTPException(status_code=409, detail="expired")


def _profile_for_run(row: dict[str, object]) -> PermissionProfile:
    """按 run 落库的档位重建运行时；老记录没有档位则用默认档。"""
    key = str(row.get("permission") or "")
    try:
        return resolve(key or None)
    except UnknownProfile:
        return default_profile()


@router.get("/run/{run_id}/pending")
def get_pending(run_id: int, session_id: str) -> dict[str, Any]:
    """刷新后恢复：拿到挂起的命令原文，重建确认卡片。

    必须带 session_id 并校验归属 —— 只凭 run_id 就能读到别人待执行的命令原文，
    等于把「即将执行的破坏性操作」当公开信息。
    """
    row = _pending_row(run_id)
    _check_pending(row, session_id)
    payload = json.loads(str(row["pending_json"] or "{}"))
    return {
        "run_id": run_id,
        "status": "parked",
        "expires_at": row["expires_at"],
        "tool_calls": payload.get("tool_calls", []),
    }


@router.post("/run/{run_id}/confirm")
async def confirm_run(run_id: int, req: ConfirmRequest, request: Request) -> StreamingResponse:
    """带着用户决策从挂起点继续跑同一个 run。

    幂等靠「条件更新」：只有把 parked 从 1 翻成 0 的那个请求才能继续，
    双击「允许一次」或两个标签页同时确认都只会执行一次。
    """
    row = _pending_row(run_id)
    _check_pending(row, req.session_id)
    profile = _profile_or_403(req.permission) if req.permission else _profile_for_run(row)
    decisions = parse_decisions([item.model_dump() for item in req.results])
    if not decisions:
        raise HTTPException(status_code=400, detail="empty_results")
    registry = _registry(request)
    if req.permission:
        # 挂起中切档：原地替换，保住 ASKING 状态；重建会把挂起的 tool_call 换成新实例
        entry, _applied = registry.switch(req.session_id, profile)
    else:
        entry = registry.acquire(req.session_id, profile)
    if not callable(getattr(entry.runtime, "resume", None)):
        raise HTTPException(status_code=400, detail="runtime_not_resumable")
    if entry.lock.locked():
        raise HTTPException(status_code=409, detail="session_busy")
    with repo.connect() as conn:
        repo.init_db(conn)
        if not repo.unpark_run(conn, run_id, now=utc_now()):
            raise HTTPException(status_code=409, detail="already_resolved")
    recorder = _recorder_for(entry, profile_key=profile.key)
    recorder.attach(run_id, req.session_id)
    await entry.lock.acquire()

    async def generate() -> AsyncIterator[str]:
        try:
            yield ": ping\n\n"
            source = entry.runtime.resume(decisions)  # type: ignore[attr-defined]
            lease = SessionLease(
                entry=entry, recorder=recorder, registry=registry, session_id=req.session_id
            )
            async for frame in _finish_or_park(lease, source):
                yield frame
        except Exception as exc:
            recorder.finish(status="failed", error=f"{type(exc).__name__}: {exc}")
            yield _sse_frame({"type": "RUN_ERROR", "error": str(exc)})
        finally:
            registry.save(req.session_id)
            entry.lock.release()
            registry.flush_pending(req.session_id)

    return StreamingResponse(generate(), media_type="text/event-stream", headers=_SSE_HEADERS)
