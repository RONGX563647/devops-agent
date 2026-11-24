"""流式端点：事件流 → SSE 帧 → 挂起或正常结束。

为什么收尾集中在这里：主链路（/run/stream）与确认续跑（confirm.py）必须对
「这一轮怎么结束」给出一致答案，否则会一边发 done、一边发 parked 地漂移。

返回 StreamingResponse 之前必须成立的不变量：

- 409 `session_busy` 检查与会话锁 acquire 都已**同步**完成。若把抢锁延迟到流内
  （例如 `__aenter__`），前端会先拿到 200 再在流里失败，重试语义也说不清。
- 锁一旦拿下只能由 `_release_lease` 释放。客户端断连抛的是 GeneratorExit /
  CancelledError（都不是 Exception 子类），所以断连兜底只能写在 finally 里，
  否则会留下永远 running 的 run。
- 断连终态沿用 failed 而不是新增 aborted：`runs.status` 的 CHECK 是建表时定的，
  老库不会因改 DDL 而放宽，新状态值会在旧库上直接 IntegrityError；
  `recorder.finish` 幂等，正常路径已写终态时不会被兜底覆盖。
- 挂起时这一条流正常结束但**不发 done**：前端据此把消息标成「等待确认」。

心跳与静默补帧的编码细节见 sse.py；挂起落库与续跑校验见 confirm.py。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from devops_agent.gateway.deps import _profile_or_403, _recorder_for, _registry
from devops_agent.gateway.input import _load_attachments, _prepare_input
from devops_agent.gateway.schemas import RunRequest
from devops_agent.gateway.sse import _SSE_HEADERS, _frames, _named_frame, _sse_frame
from devops_agent.input.events import events as parse_events
from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.confirm import ConfirmDecision
from devops_agent.runtime.runtime import AgentRuntime
from devops_agent.runtime.sessions import SessionEntry, SessionRegistry

logger = logging.getLogger(__name__)

router = APIRouter()

# 同一 run 内最多打扰用户几次。模型被拒后换个写法重试会产生新的 tool_call_id，
# 按 id 去重挡不住，所以按 run 计数；超限后自动拒绝，不再占用用户注意力。
ASK_LIMIT = 3
# 挂起有效期：用户关掉页面后不能永远停在那，超时按「未确认」处理。
PARK_TTL_SECONDS = 600

# 结清挂起时最多允许多少轮追跑：模型被拒后通常会换个方案再试一次，
# 再被拒还继续要确认就是它自己的问题，不必无限追。
SETTLE_ROUNDS = 2


@dataclass
class SessionLease:
    """一轮流式请求持有的会话租约：收尾需要的会话、记录器与注册表。"""

    entry: SessionEntry
    recorder: RunRecorder
    registry: SessionRegistry
    session_id: str


@dataclass(frozen=True)
class PreparedPayload:
    """这一轮交出去的两份输入：进模型上下文的 text 与给前端画轨迹的帧。"""

    text: str
    frames: list[dict[str, Any]]


def pending_of(runtime: AgentRuntime) -> list[dict[str, Any]]:
    """取当前仍等待确认的调用；运行时没有这能力（Echo）就是空。"""
    getter = getattr(runtime, "pending_calls", None)
    return list(getter()) if callable(getter) else []


def _expires_at() -> str:
    return (datetime.now(UTC) + timedelta(seconds=PARK_TTL_SECONDS)).isoformat()


def _bump_asks(run_id: int | None) -> int:
    """累加本 run 的确认次数；库不可用按超限处理（宁可不打扰用户）。"""
    if run_id is None:
        return ASK_LIMIT + 1
    with repo.connect() as conn:
        return repo.bump_asks(conn, run_id)


def _rejections(pending: list[dict[str, Any]]) -> list[ConfirmDecision]:
    return [
        ConfirmDecision(tool_call_id=str(item.get("tool_call_id", "")), confirmed=False)
        for item in pending
    ]


async def _settle_abandoned(runtime: AgentRuntime) -> bool:
    """把上一轮没作答的挂起按「拒绝」结清，好让新一轮能正常开跑。

    不结清就直接开跑，框架的 `_check_incoming_event` 会抛
    `Agent is waiting for N tool calls ... but received no event`，用户看到的是
    「对话运行失败」。结清方式固定是**拒绝**：没被批准的调用一条都不执行。

    模型被拒后会自己换方案，这一段追跑的输出直接丢弃（它回答的是上一个问题）。
    """
    for _ in range(SETTLE_ROUNDS):
        pending = pending_of(runtime)
        if not pending:
            return True
        resume = getattr(runtime, "resume", None)
        if not callable(resume):
            return False
        async for _frame in resume(_rejections(pending)):
            pass  # 帧不推给前端：这是上一轮的收尾，不属于这一轮的回答
    return not pending_of(runtime)


def _park(runtime: AgentRuntime, recorder: RunRecorder, pending: list[dict[str, Any]]) -> str:
    """落库挂起态并返回 `event: parked` 帧。

    关键是「挂起时流是正常结束的，不是中断」：这里不发 `event: done`，
    前端据此把消息标成等待确认而不是已完成。
    """
    expires = _expires_at()
    payload = {"tool_calls": pending, "expires_at": expires}
    recorder.park(json.dumps(payload, ensure_ascii=False), expires)
    return _named_frame(
        "parked",
        {
            "type": "RUN_PARKED",
            "run_id": recorder.run_id,
            "session_id": str(getattr(runtime, "session_id", "")),
            "expires_at": expires,
            "tool_calls": pending,
        },
    )


def _prepare_payload(req: RunRequest, recorder: RunRecorder) -> PreparedPayload:
    """解析输入：阶段事件进 recorder 并转成帧，只有 text 进模型上下文。"""
    prepared = _prepare_input(req.user_input, _load_attachments(req.attachments))
    if prepared is None:
        return PreparedPayload(text=req.user_input, frames=[])
    frames = parse_events(prepared)
    for frame in frames:
        recorder.consume(frame)  # 阶段缓存进 recorder，finish 时落库
    return PreparedPayload(text=prepared.text, frames=frames)


async def _acquire_lease(
    request: Request, req: RunRequest, x_request_id: str | None = None
) -> SessionLease:
    """同步占住会话：409 检查与 lock.acquire 都必须在返回响应前完成。"""
    profile = _profile_or_403(req.permission)
    registry = _registry(request)
    entry = registry.acquire(req.session_id, profile, req.web_search)
    if entry.lock.locked():
        raise HTTPException(status_code=409, detail="session_busy")
    # domain/model 由部署环境注入，观测数据才能反映真实运行上下文；
    # 缺省保持 debug/空串，与 Echo 自检及既有测试兼容。
    recorder = _recorder_for(entry, profile_key=profile.key, request_id=x_request_id or "")
    await entry.lock.acquire()
    return SessionLease(
        entry=entry, recorder=recorder, registry=registry, session_id=req.session_id
    )


def _release_lease(lease: SessionLease) -> None:
    """释放会话租约：断连兜底收尾 → 存快照 → 放锁 → 应用挂起的部署默认。

    顺序不能换：save 必须在放锁前完成（否则新一轮可能先写进半截状态）；
    flush_pending 会跳过仍持锁的会话，必须在放锁之后调用才会生效。
    """
    try:
        lease.recorder.finish(status="failed", error="ClientDisconnected: 客户端断开，本轮中止")
    except Exception:  # noqa: BLE001 - 收尾失败也不能阻止释放会话锁
        logger.warning("断连兜底收尾失败: %s", lease.session_id, exc_info=True)
    lease.registry.save(lease.session_id)
    lease.entry.lock.release()
    # 这一轮跑着的时候改过部署默认的话，现在补上热替换（deferred → applied）
    lease.registry.flush_pending(lease.session_id)


async def _auto_reject_over_limit(
    lease: SessionLease, pending: list[dict[str, Any]]
) -> AsyncIterator[str]:
    """超限：自动拒绝，让模型换思路，别再弹卡片。"""
    resume = getattr(lease.entry.runtime, "resume", None)
    if not callable(resume):
        return
    async for frame in _frames(resume(_rejections(pending)), lease.recorder):
        yield frame


def _done_frame(recorder: RunRecorder) -> str:
    """正常结束帧：run_id、token 汇总与最终文本。"""
    run_id = recorder.finish()
    in_tok, out_tok = recorder.token_usage
    cache_read, cache_write = recorder.cache_usage
    return _named_frame(
        "done",
        {
            "type": "RUN_DONE",
            "run_id": run_id,
            "status": "success",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            # 缓存必须与 input 同时给：只给 input 时前端拿最后一次的命中除以整轮输入，
            # 命中率会被系统性低估「调用次数」倍。
            "cache_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "text": recorder.final_text,
        },
    )


async def _finish_or_park(
    lease: SessionLease, source: AsyncIterator[dict[str, Any]]
) -> AsyncIterator[str]:
    """共用的收尾逻辑：跑事件流 → 需要确认就挂起 → 否则正常结束。

    三条收尾语义：正常 done、超限自动拒绝后再判、parked 不发 done。
    """
    async for frame in _frames(source, lease.recorder):
        yield frame
    pending = pending_of(lease.entry.runtime)
    if pending and _bump_asks(lease.recorder.run_id) > ASK_LIMIT:
        async for frame in _auto_reject_over_limit(lease, pending):
            yield frame
        pending = pending_of(lease.entry.runtime)
    if pending:
        yield _park(lease.entry.runtime, lease.recorder, pending)
        return
    yield _done_frame(lease.recorder)


@router.post("/run/stream")
async def run_stream(
    req: RunRequest,
    request: Request,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> StreamingResponse:
    """SSE 流式端点：实时推送事件，并同步把事件落库为 run + spans。

    - 每个事件一个 `data: {...}` 帧，前端可边收边渲染；
    - 结束时发 `event: done` 帧带 run_id 与汇总；
    - 需要用户确认时发 `event: parked` 帧并结束这一条流（不发 done）；
    - X-Request-ID 提供幂等键，重试不会写入重复 run。
    """
    lease = await _acquire_lease(request, req, x_request_id)
    entry, recorder = lease.entry, lease.recorder

    async def generate() -> AsyncIterator[str]:
        try:
            # 1) 建立流心跳并登记 run
            yield ": ping\n\n"  # 模型首 token 前先有字节，别让代理把空窗当空闲连接
            recorder.start(req.session_id, req.user_input)
            # 2) 结清上一轮挂起：上一轮停在 ASKING 时不清，本轮会直接 RUN_ERROR
            if pending_of(entry.runtime) and not await _settle_abandoned(entry.runtime):
                raise RuntimeError(
                    "上一次的确认还没结清（模型又被要求了一次确认），请先在卡片上作答再发新消息。"
                )
            # 3) 取 prepared payload 并推阶段帧
            prepared = _prepare_payload(req, recorder)
            for frame in prepared.frames:
                yield _sse_frame(frame)
            # 4) 转发事件流（含收尾：done / 自动拒绝 / parked）
            async for chunk in _finish_or_park(
                lease, entry.runtime.stream(req.session_id, prepared.text)
            ):
                yield chunk
        except HTTPException:
            raise
        except Exception as exc:  # 异常也要落库，记录失败
            # 原因必须一起落库：只写 status 的话，刷新后没人能复原这次为什么失败
            recorder.finish(status="failed", error=f"{type(exc).__name__}: {exc}")
            yield _sse_frame({"type": "RUN_ERROR", "error": str(exc)})
        finally:
            # 5) 统一收尾：断连（非 Exception）也在这里落库并释放锁
            _release_lease(lease)

    # 三个响应头缺一不可：no-cache 防中间层缓存整段；keep-alive 保长连接；
    # X-Accel-Buffering: no 关掉 nginx 缓冲，否则整段流会攒到结束才下发。
    return StreamingResponse(generate(), media_type="text/event-stream", headers=_SSE_HEADERS)
