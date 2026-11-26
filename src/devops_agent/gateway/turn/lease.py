"""会话租约：一轮流式请求对会话实例的独占持有权。

两条铁律：

1. **获取必须同步** —— 409 判定与 `lock.acquire()` 都在返回响应之前完成。延迟到流内
   会让前端先拿到 200 再在流里失败，重试语义说不清；
2. **释放顺序不可交换** —— 见 `SessionLease.close` 的四步。

收尾只有这一份实现：主链路与确认续跑共用，避免「主链路有断连兜底、续跑没有」的漂移。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from devops_agent.gateway.errors import ErrorCode, http_error
from devops_agent.gateway.settings import Settings
from devops_agent.gateway.turn.ports import RuntimePort
from devops_agent.governance.model import PermissionProfile
from devops_agent.observability.models import RunStatus
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.sessions import SessionEntry, SessionRegistry

logger = logging.getLogger(__name__)

# 断连沿用 failed 而不新增 aborted：runs.status 的 CHECK 建表时定死，老库加不了新值
DISCONNECT_ERROR = "ClientDisconnected: 客户端断开，本轮中止"


@dataclass
class SessionLease:
    """一轮流式请求持有的会话租约：收尾需要的会话、记录器与注册表。"""

    entry: SessionEntry
    recorder: RunRecorder
    registry: SessionRegistry
    session_id: str

    def close(self) -> None:
        """按固定顺序释放租约；四步不能重排。"""
        # 1) 断连兜底终态：断连抛 GeneratorExit（非 Exception），只有 finally 到这里；
        #    finish 幂等，正常路径已写终态时是 no-op
        try:
            self.recorder.finish(status=RunStatus.FAILED.value, error=DISCONNECT_ERROR)
        except Exception:  # noqa: BLE001 - 收尾失败也必须放锁，否则会话永久 busy
            logger.warning("断连兜底收尾失败: %s", self.session_id, exc_info=True)
        # 2) 快照落库：在放锁前，否则新一轮可能先写进半截状态
        self.registry.save(self.session_id)
        # 3) 放锁：之后的请求才能进来
        self.entry.lock.release()
        # 4) 补做本轮期间被延迟的热替换（flush_pending 会跳过持锁会话，故必须在放锁后）
        self.registry.flush_pending(self.session_id)


def claim_entry(
    registry: SessionRegistry,
    profile: PermissionProfile,
    session_id: str,
    web_search: bool | None = None,
) -> SessionEntry:
    """取会话实例并做并发前置判定（同步）。

    档位或联网开关变了会重建实例；所以「已持锁」要判 acquire 的返回值，不是旧对象。
    """
    entry = registry.acquire(session_id, profile, web_search)
    if entry.lock.locked():
        raise http_error(ErrorCode.SESSION_BUSY)
    return entry


async def hold(
    registry: SessionRegistry,
    entry: SessionEntry,
    *,
    profile_key: str,
    settings: Settings,
    request_id: str = "",
    attach_run_id: int | None = None,
) -> SessionLease:
    """建本轮记录器并拿锁。

    1) 建 recorder（带 domain / model / 参数快照）—— 放在锁外，避免持锁做 IO；
    2) 续跑时接管已有 run（沿用 started_at / seq / token 累计）；
    3) 抢锁并返回租约。
    """
    recorder = _build_recorder(
        entry, profile_key=profile_key, settings=settings, request_id=request_id
    )
    if attach_run_id is not None:
        recorder.attach(attach_run_id, entry.session_id)
    await entry.lock.acquire()
    return SessionLease(
        entry=entry,
        recorder=recorder,
        registry=registry,
        session_id=entry.session_id,
    )


async def open_lease(
    registry: SessionRegistry,
    profile: PermissionProfile,
    session_id: str,
    *,
    web_search: bool | None,
    settings: Settings,
    request_id: str = "",
) -> SessionLease:
    """主链路入口：取实例 → 409 判定 → 建记录器 → 拿锁。"""
    entry = claim_entry(registry, profile, session_id, web_search)
    return await hold(
        registry,
        entry,
        profile_key=profile.key,
        settings=settings,
        request_id=request_id,
    )


def _build_recorder(
    entry: SessionEntry,
    *,
    profile_key: str,
    settings: Settings,
    request_id: str = "",
) -> RunRecorder:
    """建本轮记录器，带上实际下发的参数快照（参数可配后，不进记录就不可归因）。"""
    return RunRecorder(
        domain=settings.domain(),
        model=settings.model(),
        request_id=request_id,
        permission=profile_key,
        model_params=RuntimePort(entry.runtime).params_record(),
    )
