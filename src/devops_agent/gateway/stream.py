"""流式端点：事件流 → SSE 帧 → 挂起或正常结束。

挂起相关的助手也放在这里：它们决定的都是「这条流怎么收尾」，与续跑端点
（confirm.py）共用同一套实现，避免两套收尾逻辑慢慢长歪。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
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
from devops_agent.runtime.sessions import SessionEntry

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

    为什么非有这一步：卡片弹出后用户可能直接点中断或关掉页面，前端只是 abort
    了请求，**服务端仍停在 ASKING**。这时再发一条消息，框架的
    `_check_incoming_event` 发现「等着 N 个调用、进来的事件里却没有确认结果」，
    直接抛 `Agent is waiting for N tool calls ... but received no event` ——
    用户看到的就是「对话运行失败」「怎么搞的你」。

    结清方式固定是**拒绝**：没被批准的调用一条都不执行，发条消息不会顺手放行。
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


async def _stream_entry(
    entry: SessionEntry,
    recorder: RunRecorder,
    source: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[str]:
    """共用的收尾逻辑：跑事件流 → 需要确认就挂起 → 否则正常结束。

    主链路与「确认后续跑」都走这里，避免两套收尾实现慢慢长歪。
    """
    async for frame in _frames(source, recorder):
        yield frame
    pending = pending_of(entry.runtime)
    if pending and _bump_asks(recorder.run_id) > ASK_LIMIT:
        # 超限：自动拒绝，让模型换思路，别再弹卡片
        resume = getattr(entry.runtime, "resume", None)
        if callable(resume):
            async for frame in _frames(resume(_rejections(pending)), recorder):
                yield frame
            pending = pending_of(entry.runtime)
    if pending:
        yield _park(entry.runtime, recorder, pending)
        return
    run_id = recorder.finish()
    in_tok, out_tok = recorder.token_usage
    cache_read, cache_write = recorder.cache_usage
    yield _named_frame(
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
    profile = _profile_or_403(req.permission)
    registry = _registry(request)
    entry = registry.acquire(req.session_id, profile, req.web_search)
    if entry.lock.locked():
        raise HTTPException(status_code=409, detail="session_busy")
    # domain/model 由部署环境注入，观测数据才能反映真实运行上下文；
    # 缺省保持 debug/空串，与 Echo 自检及既有测试兼容。
    recorder = _recorder_for(entry, profile_key=profile.key, request_id=x_request_id or "")
    await entry.lock.acquire()

    async def generate() -> AsyncIterator[str]:
        try:
            # 首帧注释心跳：模型首 token 常需 2~3 秒，先让代理与浏览器确认流已建立，
            # 避免这段推理空窗被当成空闲连接断开（SSE 注释帧前端会忽略）。
            yield ": ping\n\n"
            recorder.start(req.session_id, req.user_input)
            # 上一轮可能停在 ASKING（用户点了中断/关了页面），不结清就直接开跑
            # 会撞上框架的「waiting for N tool calls but received no event」，
            # 整轮变成 RUN_ERROR。先按拒绝结清，再开始这一轮。
            if pending_of(entry.runtime) and not await _settle_abandoned(entry.runtime):
                raise RuntimeError(
                    "上一次的确认还没结清（模型又被要求了一次确认），请先在卡片上作答再发新消息。"
                )
            # 输入解析在真正调用 Agent 之前完成：事件照常推给前端画轨迹，
            # 但只有 prepared.text 会进入模型上下文——密钥与裁剪细节不外泄。
            payload = req.user_input
            prepared = _prepare_input(req.user_input, _load_attachments(req.attachments))
            if prepared is not None:
                payload = prepared.text
                for frame in parse_events(prepared):
                    recorder.consume(frame)  # 阶段缓存进 recorder，finish 时落库
                    yield _sse_frame(frame)
            async for chunk in _stream_entry(
                entry, recorder, entry.runtime.stream(req.session_id, payload)
            ):
                yield chunk
        except HTTPException:
            raise
        except Exception as exc:  # 异常也要落库，记录失败
            # 原因必须一起落库：只写 status 的话，刷新后没人能复原这次为什么失败
            recorder.finish(status="failed", error=f"{type(exc).__name__}: {exc}")
            yield _sse_frame({"type": "RUN_ERROR", "error": str(exc)})
        finally:
            # 客户端断连抛的是 GeneratorExit / CancelledError，都不是 Exception 子类，
            # 上面的 except 抓不到；不兜底就会留下永远 running 的 run。
            # finish 幂等（正常路径已写过 success/failed 时直接早退），不会覆盖终态。
            # 终态沿用 failed 而不是新增 aborted：runs.status 的 CHECK 是建表时定的，
            # 老库不会因为改 DDL 而放宽，新写一个状态值会在旧库上直接 IntegrityError。
            try:
                recorder.finish(status="failed", error="ClientDisconnected: 客户端断开，本轮中止")
            except Exception:  # noqa: BLE001 - 收尾失败也不能阻止释放会话锁
                logger.warning("断连兜底收尾失败: %s", req.session_id, exc_info=True)
            # 会话快照要包含这一轮结束后的完整状态，刷新后重建才不会丢历史
            registry.save(req.session_id)
            entry.lock.release()
            # 这一轮跑着的时候改过部署默认的话，现在补上热替换（deferred → applied）
            registry.flush_pending(req.session_id)

    # 三个响应头都是流式能被"真的是流"看到的前提：
    # - no-cache：避免中间层缓存整段响应
    # - keep-alive：保持长连接，别在帧间隙被关闭
    # - X-Accel-Buffering: no：关闭 nginx 等反向代理的响应缓冲，
    #   否则代理会把整段流攒到结束才下发，前端又变回一次性渲染
    return StreamingResponse(generate(), media_type="text/event-stream", headers=_SSE_HEADERS)
