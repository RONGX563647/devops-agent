"""挂起查询与确认续跑 —— 只做 HTTP 契约映射与幂等。

1. **客户端不回传命令原文**：只给 `tool_call_id + confirmed + scope`，原文以
   `runs.pending_json` 为权威，否则等于允许客户端改写将要执行的命令；
2. **幂等靠条件 UPDATE**：把 `parked` 从 1 翻成 0 的那个请求才能继续 —— 双击
   「允许一次」、两个标签页同时确认都只执行一次。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from devops_agent.gateway.contract import PENDING_STATUS, SSE_HEADERS, SSE_MEDIA_TYPE
from devops_agent.gateway.deps import profile_or_403, registry_of, settings_of
from devops_agent.gateway.ending import RunLimits
from devops_agent.gateway.errors import ErrorCode, http_error
from devops_agent.gateway.lease import claim_entry, hold
from devops_agent.gateway.ports import PendingRun, PendingStore, RuntimePort
from devops_agent.gateway.run import resume_turn
from devops_agent.gateway.schemas import ConfirmRequest
from devops_agent.governance.profiles import (
    PermissionProfile,
    UnknownProfile,
    default_profile,
    resolve,
)
from devops_agent.observability.recorder import utc_now
from devops_agent.runtime.confirm import parse_decisions
from devops_agent.runtime.sessions import SessionEntry, SessionRegistry

router = APIRouter()


def _pending_or_4xx(store: PendingStore, run_id: int, session_id: str) -> PendingRun:
    """读挂起记录并做归属 / 状态 / 过期三项校验。

    三项都必须查，且顺序固定：不存在(404) → 不是你的(403) → 已结清/已过期(409)。
    先查归属是为了不泄露「这条 run 存在」这个事实。
    """
    row = store.load(run_id)
    if row is None:
        raise http_error(ErrorCode.RUN_NOT_FOUND)
    if not row.owned_by(session_id):
        raise http_error(ErrorCode.SESSION_MISMATCH)
    if not row.parked:
        raise http_error(ErrorCode.ALREADY_RESOLVED)
    if row.is_expired(utc_now()):
        raise http_error(ErrorCode.EXPIRED)
    return row


def _profile_for_run(row: PendingRun) -> PermissionProfile:
    """按 run 落库的档位重建运行时；老记录没有档位则用部署默认档。"""
    try:
        return resolve(row.permission or None)
    except UnknownProfile:
        return default_profile()


@router.get("/run/{run_id}/pending")
def get_pending(run_id: int, session_id: str) -> dict[str, Any]:
    """刷新后恢复：拿到挂起的命令原文，重建确认卡片。

    必须带 session_id 并校验归属 —— 只凭 run_id 就能读到别人待执行的命令原文，
    等于把「即将执行的破坏性操作」当公开信息。
    """
    row = _pending_or_4xx(PendingStore(), run_id, session_id)
    return {
        "run_id": run_id,
        "status": PENDING_STATUS,
        "expires_at": row.expires_at,
        "tool_calls": row.tool_calls,
    }


@router.post("/run/{run_id}/confirm")
async def confirm_run(run_id: int, req: ConfirmRequest, request: Request) -> StreamingResponse:
    """带着用户决策从挂起点继续跑同一个 run。

    1) 读挂起态并校验（404 / 403 / 409 都在返回响应前给出）；
    2) 定档位：请求带了就用它（挂起中切档），否则按 run 落库的档位重建；
    3) 校验决策非空，非法 scope 在 parse_decisions 里降级为 once；
    4) 取会话实例：带档位走 switch（原地热替换，保住 ASKING 状态），否则 acquire；
    5) 能力检查：运行时必须能 resume，否则 400（不是流里的错误帧）；
    6) 幂等解挂：条件 UPDATE 失败说明别人已经确认过，409；
    7) 拿锁并接管记录器，把帧流交给 run.resume_turn。
    """
    # 1) 前置校验
    store = PendingStore()
    row = _pending_or_4xx(store, run_id, req.session_id)
    # 2) 档位：确认卡上切过档就以当前请求为准，不再锁死发起轮
    profile = _profile_or_4xx(req, row)
    # 3) 决策
    decisions = parse_decisions([item.model_dump() for item in req.results])
    if not decisions:
        raise http_error(ErrorCode.EMPTY_RESULTS)
    # 4) 会话实例 + 并发判定
    registry = registry_of(request)
    entry = _entry_for(req, registry, profile)
    # 5) 运行时能力：没有 resume 就不能续跑，早失败好过在流里空转
    if not RuntimePort(entry.runtime).resumable:
        raise http_error(ErrorCode.RUNTIME_NOT_RESUMABLE)
    # 6) 解挂（幂等）：失败即说明已被别的请求确认或已过期
    if not store.resolve(run_id, now=utc_now()):
        raise http_error(ErrorCode.ALREADY_RESOLVED)
    # 7) 接管同一个 run：沿用 started_at / seq / token 累计，续跑不新开记录
    settings = settings_of(request)
    lease = await hold(
        registry,
        entry,
        profile_key=profile.key,
        settings=settings,
        attach_run_id=run_id,
    )
    return StreamingResponse(
        resume_turn(
            lease=lease,
            port=RuntimePort(entry.runtime),
            store=store,
            limits=RunLimits.from_policy(settings.stream),
            decisions=decisions,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers=dict(SSE_HEADERS),
    )


def _profile_or_4xx(req: ConfirmRequest, row: PendingRun) -> PermissionProfile:
    """挂起中切档：请求里的档位优先，且仍然要过白名单（不能靠这个端点提权）。"""
    if not req.permission:
        return _profile_for_run(row)
    return profile_or_403(req.permission)


def _entry_for(
    req: ConfirmRequest, registry: SessionRegistry, profile: PermissionProfile
) -> SessionEntry:
    """取会话实例：切档必须原地热替换，不能走 acquire 重建。

    `acquire()` 的语义是「按这次请求的档位拿到实例」，档位不同就重建 —— 重建会把
    挂起中的 tool_call 换成新实例上的空状态，确认时找不到它。
    """
    if req.permission:
        entry, _applied = registry.switch(req.session_id, profile)
        if entry.lock.locked():
            raise http_error(ErrorCode.SESSION_BUSY)
        return entry
    return claim_entry(registry, profile, req.session_id)
