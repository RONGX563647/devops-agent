"""一轮 run 的编排：骨架固定，两个入口只提供各自的业务主体。

- `run_turn`：登记 run → 结清遗留挂起 → 解析输入 → 转发事件 → 收尾；
- `resume_turn`：带用户决策续跑，不重发输入解析帧。

两者共用 `_guarded`（心跳 / 异常 / 释放）与 `finish_or_park`（三种结束语义），
所以「这一轮怎么结束」只有一处实现。本模块不碰 HTTP —— 状态码与响应头在
stream.py / confirm.py，帧字段在 contract.py。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from devops_agent.gateway.contract import (
    PING_FRAME,
    PendingPayload,
    TokenUsage,
    done_frame,
    error_frame,
    parked_frame,
    sse_frame,
)
from devops_agent.gateway.ending import Ending, RunLimits, decide_ending
from devops_agent.gateway.input_prep import (
    load_attachments,
    prepare_input,
    redact_for_record,
)
from devops_agent.gateway.lease import SessionLease
from devops_agent.gateway.ports import PendingStore, RuntimePort
from devops_agent.gateway.sse import frames
from devops_agent.input.events import events as parse_events
from devops_agent.observability.models import RunStatus
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.confirm import ConfirmDecision

logger = logging.getLogger(__name__)

# 只在「上一轮卡片没作答」时出现：说明问题在上一轮，不是这一轮失败
SETTLE_FAILED = "上一次的确认还没结清（模型又被要求了一次确认），请先在卡片上作答再发新消息。"


@dataclass(frozen=True, slots=True)
class PreparedPayload:
    """这一轮交出去的两份输入：进模型上下文的 text 与给前端画轨迹的帧。"""

    text: str
    frames: list[dict[str, Any]]


# --- 两个入口 -------------------------------------------------------------


def run_turn(
    *,
    lease: SessionLease,
    port: RuntimePort,
    store: PendingStore,
    limits: RunLimits,
    session_id: str,
    user_input: str,
    attachments: Sequence[str] = (),
) -> AsyncIterator[str]:
    """主链路的一轮；返回帧流（同步构造，调用方可以先返回响应头）。"""

    async def body() -> AsyncIterator[str]:
        # 1) 登记 run：input 用脱敏正文（明文 key 不能持久化，也不能经 /api/runs 回前端）
        lease.recorder.start(session_id, redact_for_record(user_input))
        # 2) 结清上一轮遗留挂起：不清就跑，框架会抛「waiting for N tool calls」
        if not await settle_abandoned(port, limits):
            raise RuntimeError(SETTLE_FAILED)
        # 3) 解析输入：阶段帧推前端 + 缓存进 recorder；只有 text 进模型上下文
        prepared = prepare_payload(user_input, list(attachments), lease.recorder)
        for frame in prepared.frames:
            yield sse_frame(frame)
        # 4) 转发事件流，末尾按「自动拒绝 / 挂起 / 正常结束」收尾
        async for chunk in finish_or_park(
            lease=lease,
            port=port,
            store=store,
            limits=limits,
            source=port.runtime.stream(session_id, prepared.text),
        ):
            yield chunk

    return _guarded(lease, body)


def resume_turn(
    *,
    lease: SessionLease,
    port: RuntimePort,
    store: PendingStore,
    limits: RunLimits,
    decisions: Sequence[ConfirmDecision],
) -> AsyncIterator[str]:
    """确认续跑：接上同一个 run，收尾语义与主链路完全一致。

    1) 记录器已由 `lease.hold(attach_run_id=...)` 接管，这里不重开 run；
    2) 决策交给运行时续跑，其余全部交给 `finish_or_park`。
    """

    async def body() -> AsyncIterator[str]:
        async for chunk in finish_or_park(
            lease=lease,
            port=port,
            store=store,
            limits=limits,
            source=port.resume(decisions),
        ):
            yield chunk

    return _guarded(lease, body)


# --- 骨架：心跳 / 异常 / 释放 --------------------------------------------


async def _guarded(
    lease: SessionLease, body: Callable[[], AsyncIterator[str]]
) -> AsyncIterator[str]:
    """所有流式端点的统一外壳。

    1) 先发心跳注释：模型首 token 前需要字节，别让代理把空窗当空闲连接；
    2) 跑主体；
    3) 异常 → 带原因落库（只写 status 的话，刷新后没人能复原为什么失败）+ 一帧 RUN_ERROR；
    4) finally 释放租约（断连抛 GeneratorExit，只有 finally 兜得住）。
    """
    try:
        yield PING_FRAME
        async for frame in body():
            yield frame
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落库并转成前端可读的帧
        lease.recorder.finish(status=RunStatus.FAILED.value, error=f"{type(exc).__name__}: {exc}")
        yield error_frame(str(exc))
    finally:
        lease.close()


# --- 步骤实现 -------------------------------------------------------------


async def settle_abandoned(port: RuntimePort, limits: RunLimits) -> bool:
    """把上一轮没作答的挂起按「拒绝」结清，返回是否结清干净。

    1) 没有待确认调用 → 已干净，直接开跑；
    2) 有则整批拒绝并追跑 —— 没被批准的调用一条都不执行；
    3) 追跑到上限仍未清空（或运行时不能续跑）→ 返回 False，由调用方转成失败。
    """
    for _ in range(limits.settle_rounds):
        pending = port.pending()
        if not pending:
            return True
        if not port.resumable:
            return False
        # 帧直接丢弃：这段追跑在回答上一个问题，不属于这一轮
        async for _frame in port.resume(_rejections(pending)):
            pass
    return not port.pending()


def prepare_payload(
    user_input: str, attachments: Sequence[str], recorder: RunRecorder
) -> PreparedPayload:
    """解析输入：阶段事件进 recorder 并转成帧，只有 text 进模型（失败退回原文）。"""
    prepared = prepare_input(user_input, load_attachments(list(attachments)))
    if prepared is None:
        return PreparedPayload(text=user_input, frames=[])
    stage_frames = parse_events(prepared)
    for frame in stage_frames:
        recorder.consume(frame)  # 阶段缓存进 recorder，finish 时随 run 一起落库
    return PreparedPayload(text=prepared.text, frames=stage_frames)


async def finish_or_park(
    *,
    lease: SessionLease,
    port: RuntimePort,
    store: PendingStore,
    limits: RunLimits,
    source: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[str]:
    """三条收尾语义的唯一实现；判定是纯函数，这里只按结果分派副作用。

    1) 转发事件流（含心跳补帧）；
    2) 取挂起调用：没有就正常结束；
    3) 有则计一次打扰，超限就先整批拒绝再重判；
    4) 重判仍有挂起 → 落库挂起并结束这条流（不发 done）；
    5) 否则落终态并发 done。
    """
    async for frame in frames(source, lease.recorder, heartbeat=limits.heartbeat_seconds):
        yield frame
    pending = port.pending()
    if pending:
        asks = store.bump_asks(lease.recorder.run_id)
        if decide_ending(pending=pending, asks=asks, limits=limits) is Ending.OVER_ASK_LIMIT:
            # 超限：自动拒绝让模型换思路；这次追跑的输出算本轮回答，照常转发
            async for frame in _reject_over_limit(port, lease.recorder, limits, pending):
                yield frame
            pending = port.pending()
    if not pending:
        yield finish_frame(lease.recorder)
        return
    yield park(lease, port, limits)


def finish_frame(recorder: RunRecorder) -> str:
    """落终态并返回 done 帧 —— 先 finish 再拼帧，run_id 与用量以落库结果为准。"""
    run_id = recorder.finish()
    return done_frame(run_id=run_id, text=recorder.final_text, usage=usage_of(recorder))


async def _reject_over_limit(
    port: RuntimePort,
    recorder: RunRecorder,
    limits: RunLimits,
    pending: Sequence[Mapping[str, Any]],
) -> AsyncIterator[str]:
    """超限后的自动拒绝：把拒绝当决策交给运行时，让模型换方案重来。"""
    if not port.resumable:
        return
    async for frame in frames(
        port.resume(_rejections(pending)), recorder, heartbeat=limits.heartbeat_seconds
    ):
        yield frame


def park(lease: SessionLease, port: RuntimePort, limits: RunLimits) -> str:
    """落库挂起态并返回 parked 帧。

    1) 组装载荷（含截止时刻）；
    2) 写 runs.parked / pending_json；
    3) 发帧 —— 不发 done，前端据此标「等待确认」而不是「已完成」。
    """
    payload = PendingPayload(tool_calls=port.pending(), expires_at=limits.expires_at())
    lease.recorder.park(payload.to_json(), payload.expires_at)
    return parked_frame(
        run_id=lease.recorder.run_id,
        session_id=lease.session_id,
        expires_at=payload.expires_at,
        tool_calls=payload.tool_calls,
    )


def usage_of(recorder: RunRecorder) -> TokenUsage:
    """把记录器的两个累加量收成一个值对象（帧渲染只认这个形状）。"""
    input_tokens, output_tokens = recorder.token_usage
    cache_read, cache_write = recorder.cache_usage
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def _rejections(pending: Sequence[Mapping[str, Any]]) -> list[ConfirmDecision]:
    """把一批待确认调用统一翻成「拒绝」决策。"""
    return [
        ConfirmDecision(tool_call_id=str(item.get("tool_call_id", "")), confirmed=False)
        for item in pending
    ]
