"""会话租约 —— 一轮流式请求对会话实例的独占持有权。

两条铁律，改这个文件前先读一遍：

1. **获取必须同步完成**。「409 会话忙」的判定与 `lock.acquire()` 都要在
   `return StreamingResponse(...)` 之前跑完。若把抢锁延迟到流内，前端会先拿到 200
   再在流里失败，重试语义说不清。
2. **释放顺序不可交换**（见 `SessionLease.close` 的四步）。

为什么单独成模块：主链路（`/run/stream`）与确认续跑（`/run/{id}/confirm`）都要
「占住—收尾」，改造前各写一份，于是续跑那条少了断连兜底（客户端在续跑途中断开时，
run 会停在已解挂、`ended_at` 为空的悬空态）。现在收尾只有这一份实现。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from devops_agent.gateway.errors import ErrorCode, http_error
from devops_agent.gateway.ports import RuntimePort
from devops_agent.gateway.settings import Settings
from devops_agent.governance.profiles import PermissionProfile
from devops_agent.observability.models import RunStatus
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.sessions import SessionEntry, SessionRegistry

logger = logging.getLogger(__name__)

# 断连的兜底终态沿用 failed 而不是新增 aborted：runs.status 的 CHECK 是建表时定的，
# 老库不会因改 DDL 而放宽，新状态值会在旧库上直接 IntegrityError。
DISCONNECT_ERROR = "ClientDisconnected: 客户端断开，本轮中止"


@dataclass
class SessionLease:
    """一轮流式请求持有的会话租约：收尾需要的会话、记录器与注册表。"""

    entry: SessionEntry
    recorder: RunRecorder
    registry: SessionRegistry
    session_id: str

    def close(self) -> None:
        """释放租约。四步顺序各有理由，不能重排。"""
        # 1) 断连兜底终态：客户端断开抛 GeneratorExit / CancelledError（都不是
        #    Exception），只有 finally 能走到这里；finish 幂等，正常路径已写终态即 no-op
        try:
            self.recorder.finish(status=RunStatus.FAILED.value, error=DISCONNECT_ERROR)
        except Exception:  # noqa: BLE001 - 收尾失败也不能阻止放锁，否则会话永久 busy
            logger.warning("断连兜底收尾失败: %s", self.session_id, exc_info=True)
        # 2) 快照落库：必须在放锁前，否则新一轮可能先写进半截状态
        self.registry.save(self.session_id)
        # 3) 放锁：之后的请求才能进来
        self.entry.lock.release()
        # 4) 补做本轮期间被延迟的热替换：flush_pending 会跳过仍持锁的会话，
        #    所以它必须在放锁之后调用才会生效（deferred → applied）
        self.registry.flush_pending(self.session_id)


def claim_entry(
    registry: SessionRegistry,
    profile: PermissionProfile,
    session_id: str,
    web_search: bool | None = None,
) -> SessionEntry:
    """取会话实例并做并发前置判定（同步，不 await）。

    档位或联网开关变了会重建实例（从 DB 快照恢复历史）；重建后的新实例不会有锁，
    所以「已持锁」判定必须针对 acquire 的**返回值**，而不是调用前的旧对象。
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
    """建本轮记录器并拿锁；`attach_run_id` 用于确认续跑接管已有 run。

    记录器在拿锁前建好：409 已经判过，此刻失败没有并发风险；拿锁后再做 IO
    只会把锁的持有时间拉长。
    """
    recorder = _build_recorder(
        entry, profile_key=profile_key, settings=settings, request_id=request_id
    )
    if attach_run_id is not None:
        # 续跑：沿用库里的 started_at / seq / token 累计，`RUN_DONE` 才是整轮累积结果
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
    """主链路的租约入口：取实例 → 409 判定 → 建记录器 → 拿锁。"""
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
    """建一次运行的记录器，并把本轮实际下发的参数快照一起带上。

    参数不进 run 记录，一旦可配就不可归因：健康分与评测批次会把「参数变化点前后」
    混在一起算。取不到（Echo）时留空 dict，界面上显示「未记录」而不是猜一个默认值。
    """
    return RunRecorder(
        domain=settings.domain(),
        model=settings.model(),
        request_id=request_id,
        permission=profile_key,
        model_params=RuntimePort(entry.runtime).params_record(),
    )
