"""一轮 run 的编排 —— 从「已占住会话」到「三种结束方式」的固定顺序。

这个模块是模板方法：**骨架固定，两个入口只提供各自的业务主体**。
- 主链路 `run_turn`：登记 run → 结清遗留挂起 → 解析输入 → 转发事件 → 收尾；
- 续跑 `resume_turn`：带着用户决策继续，不重发输入解析帧。

两者共用 `_guarded`（心跳 / 异常→帧 / 收尾）与 `finish_or_park`（三种结束语义），
所以「这一轮怎么结束」只有一处实现 —— 改造前主链路与续跑各写一份，续跑那份少了
断连兜底，客户端断开时 run 会悬空。

本模块不碰 HTTP：没有 Request、没有状态码、没有响应头。契约映射在 stream.py /
confirm.py，帧的字段在 contract.py。
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

# 结清失败时给用户看的话：这条消息只在「模型反复要求确认」时出现，
# 说明上一轮的卡片还没作答，而不是这一轮出了问题。
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
    """主链路的一轮。返回帧流（同步构造，保证调用方可以先返回响应头）。"""

    async def body() -> AsyncIterator[str]:
        # 1) 登记 run：input 用脱敏正文 —— 用户贴进聊天框的 key 不能明文持久化，
        #    也不能经 /api/runs 原样回到前端；管线那侧仍用原文走完整解析
        lease.recorder.start(session_id, redact_for_record(user_input))
        # 2) 结清上一轮遗留挂起：不清就跑，框架会抛
        #    「Agent is waiting for N tool calls ... but received no event」
        if not await settle_abandoned(port, limits):
            raise RuntimeError(SETTLE_FAILED)
        # 3) 解析输入：阶段帧推前端 + 缓存进 recorder，只有 text 进模型上下文
        prepared = prepare_payload(user_input, list(attachments), lease.recorder)
        for frame in prepared.frames:
            yield sse_frame(frame)
        # 4) 转发运行时事件流，并在末尾按「挂起 / 自动拒绝 / 正常结束」收尾
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
    """确认续跑：接上同一个 run 继续跑，收尾语义与主链路完全一致。"""

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


# --- 骨架：心跳 / 异常 / 收尾 --------------------------------------------


async def _guarded(
    lease: SessionLease, body: Callable[[], AsyncIterator[str]]
) -> AsyncIterator[str]:
    """所有流式端点的统一外壳。

    1) 先发一帧心跳注释：模型首 token 要 2~3 秒，先给代理一点字节，别让空窗被当空闲连接；
    2) 跑主体；
    3) 异常 → 落库（带原因）+ 一帧 RUN_ERROR；
    4) finally → 释放租约（断连抛的 GeneratorExit 不是 Exception，只有 finally 兜得住）。
    """
    try:
        yield PING_FRAME
        async for frame in body():
            yield frame
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落库并转成前端可读的帧
        # 原因必须一起落库：只写 status 的话，刷新后没人能复原这次为什么失败
        lease.recorder.finish(status=RunStatus.FAILED.value, error=f"{type(exc).__name__}: {exc}")
        yield error_frame(str(exc))
    finally:
        lease.close()


# --- 步骤实现 -------------------------------------------------------------


async def settle_abandoned(port: RuntimePort, limits: RunLimits) -> bool:
    """把上一轮没作答的挂起按「拒绝」结清，返回是否结清干净。

    结清方式固定是拒绝：没被批准的调用一条都不执行。
    模型被拒后会自己换方案，这段追跑的输出**直接丢弃**（它回答的是上一个问题），
    所以这里只 drain 不转发。追跑上限来自配置（settle_rounds）。
    """
    for _ in range(limits.settle_rounds):
        pending = port.pending()
        if not pending:
            return True
        if not port.resumable:
            return False
        async for _frame in port.resume(_rejections(pending)):
            pass  # 帧不推给前端：这是上一轮的收尾，不属于这一轮的回答
    return not port.pending()


def prepare_payload(
    user_input: str, attachments: Sequence[str], recorder: RunRecorder
) -> PreparedPayload:
    """解析输入：阶段事件进 recorder 并转成帧，只有 text 进模型上下文。

    解析失败退回原文（`input_prep.prepare_input` 保证不抛），此时没有阶段帧。
    """
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
    """三条收尾语义的唯一实现：正常 done / 超限自动拒绝后再判 / 挂起不发 done。

    判定逻辑是纯函数（ending.decide_ending），这里只按结果分派副作用：
    写库（bump / park）、拼帧、发 done。
    """
    async for frame in frames(source, lease.recorder, heartbeat=limits.heartbeat_seconds):
        yield frame
    pending = port.pending()
    if pending:
        asks = store.bump_asks(lease.recorder.run_id)
        if decide_ending(pending=pending, asks=asks, limits=limits) is Ending.OVER_ASK_LIMIT:
            # 超限：自动拒绝让模型换思路，别再弹卡片；这次追跑的输出算本轮回答
            async for frame in _reject_over_limit(port, lease.recorder, limits, pending):
                yield frame
            pending = port.pending()
    if not pending:
        yield finish_frame(lease.recorder)
        return
    yield park(lease, port, limits)


def finish_frame(recorder: RunRecorder) -> str:
    """落终态并返回 done 帧。

    顺序不能反：`finish()` 回填 status / output / token 汇总并返回 run_id，
    帧里的 run_id 与用量都应以它为准。先拼帧再落库会让「前端看到 done、
    库里还是 running」这种半截状态出现，也让 close() 的断连兜底把成功覆盖成 failed。
    """
    run_id = recorder.finish()
    return done_frame(run_id=run_id, text=recorder.final_text, usage=usage_of(recorder))


async def _reject_over_limit(
    port: RuntimePort,
    recorder: RunRecorder,
    limits: RunLimits,
    pending: Sequence[Mapping[str, Any]],
) -> AsyncIterator[str]:
    """超限后的自动拒绝：把拒绝当决策传给运行时，让模型换方案重来。"""
    if not port.resumable:
        return
    async for frame in frames(
        port.resume(_rejections(pending)), recorder, heartbeat=limits.heartbeat_seconds
    ):
        yield frame


def park(lease: SessionLease, port: RuntimePort, limits: RunLimits) -> str:
    """落库挂起态并返回 parked 帧。

    关键是「挂起时流是正常结束的，不是中断」：这里不发 done，前端据此把消息标成
    等待确认而不是已完成。
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
