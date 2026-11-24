"""应用入口：装配运行时与会话注册表，并启动 FastAPI 网关。"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from devops_agent.env import load_env
from devops_agent.governance.profiles import (
    PermissionProfile,
    UnknownProfile,
    available,
    default_profile,
    resolve,
)
from devops_agent.input.api import router as attachments_router
from devops_agent.input.attachments import Attachment, AttachmentStore
from devops_agent.input.events import events as parse_events
from devops_agent.input.pipeline import PreparedInput, prepare
from devops_agent.observability import repository as repo
from devops_agent.observability.api import router as observability_router
from devops_agent.observability.eval_api import router as eval_router
from devops_agent.observability.recorder import RunRecorder, utc_now
from devops_agent.runtime.agent import build_agentscope_runtime, session_runtime_factory
from devops_agent.runtime.confirm import ConfirmDecision, parse_decisions
from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime
from devops_agent.runtime.sessions import SessionEntry, SessionRegistry
from devops_agent.secrets.api import router as secrets_router
from devops_agent.secrets.vault import get_vault
from devops_agent.tools.api import router as tools_router
from devops_agent.tools.context_api import router as context_router
from devops_agent.tools.preflight import health_payload, missing_tools

# 同一 run 内最多打扰用户几次。模型被拒后换个写法重试会产生新的 tool_call_id，
# 按 id 去重挡不住，所以按 run 计数；超限后自动拒绝，不再占用用户注意力。
ASK_LIMIT = 3
# 挂起有效期：用户关掉页面后不能永远停在那，超时按「未确认」处理。
PARK_TTL_SECONDS = 600

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def select_runtime() -> AgentRuntime:
    """按环境变量选择运行时：配齐凭据用真实模型，否则回退 Echo（便于自检/测试）。"""
    if os.environ.get("DEVOPS_AGENT_API_KEY"):
        return build_agentscope_runtime()
    return EchoRuntime()


app = FastAPI(title="devops-agent")
# runtime 挂在 app.state（FastAPI 标准注入容器）而非裸模块变量：
# 测试 / 部署可替换 app.state.runtime，无需改环境变量或重启进程。
# 只在进程启动时加载一次 .env；放在 select_runtime 之前保证凭据就绪。
# 不放进 select_runtime 内部——否则测试里 monkeypatch.delenv 会被 .env 重新填回。
load_env()
app.state.runtime = select_runtime()
# 会话注册表懒建：单测替换 app.state.runtime 后，下一次请求自然用新实例做工厂
app.state.sessions = None
app.state.sessions_base = None


def runtime_status(runtime: AgentRuntime | None = None) -> dict[str, str]:
    """当前运行时的自述，供 `/health` 与前端横幅使用。

    为什么必须暴露：Echo 兜底的回复是 `[<session>] <输入>`，看起来「像在回答
    但答非所问」。没有这个字段时，「没配 key」和「agent 坏了」在界面上完全
    无法区分，用户只能反复重试（实测就因此产生了同一条输入的两个 run）。
    """
    current = app.state.runtime if runtime is None else runtime
    if isinstance(current, EchoRuntime):
        return {
            "kind": "echo",
            "model": "",
            "notice": "未配置模型凭据（DEVOPS_AGENT_API_KEY），当前回复只是回显输入，不会调用模型",
        }
    return {
        "kind": "agentscope",
        "model": os.environ.get("DEVOPS_AGENT_MODEL", ""),
        "notice": "",
    }


logger = logging.getLogger(__name__)


def _warn_missing_tools() -> None:
    """启动时把缺失的外部依赖喊出来。

    只在进程启动说一次，而不是等每次调用失败才说：模型看到的是工具报错，
    运维看到的是这条日志，两边说的是同一件事，总得有人先把它讲清楚。
    不要把这件事做成 fail-fast —— 缺 `rg` 只影响一个工具，没必要拒绝启动。
    """
    missing = missing_tools()
    if missing:
        logger.warning(
            "外部依赖缺失，以下工具会持续失败：%s；安装方式见 GET /api/tools/preflight",
            ", ".join(missing),
        )


_warn_missing_tools()


def _warn_runtime() -> None:
    """降级到 Echo 只打日志，不拒绝启动 —— 自检和测试都要用 Echo。

    但绝不能一声不响：这条 warning 是排查「agent 突然变傻」时唯一的线索。
    """
    status = runtime_status()
    if status["kind"] == "echo":
        logger.warning("%s", status["notice"])


_warn_runtime()
app.include_router(observability_router)
app.include_router(eval_router)
app.include_router(attachments_router)
app.include_router(secrets_router)
app.include_router(tools_router)
app.include_router(context_router)


class RunRequest(BaseModel):
    session_id: str
    user_input: str
    # 附件 id 列表（由 POST /api/attachments 返回）：正文留服务端，只在这里带 id
    attachments: list[str] = Field(default_factory=list)
    # 权限档位；省略用服务端默认档
    permission: str | None = None
    # 逐请求的联网搜索开关：默认关闭，前端「联网」按钮控制
    web_search: bool = False


class ConfirmItem(BaseModel):
    """前端回的确认结果：只给 id 与决策，命令原文以服务端挂起态为准。"""

    tool_call_id: str
    confirmed: bool
    # once / exact / prefix，非法值在 parse_decisions 里降级为 once
    scope: str = "once"


class ConfirmRequest(BaseModel):
    session_id: str
    results: list[ConfirmItem] = Field(default_factory=list)


@app.get("/health")
def health() -> dict[str, Any]:
    """存活 + 运行时模式 + 工具外部依赖就绪状态。

    缺依赖**不返回 5xx**：探针看到 5xx 会重启容器，而重启治不好缺少的可执行
    文件，只会把一次可诊断的降级放大成重启循环。所以状态放在 body 的
    `tools` 里，由调用方判断要不要报警。

    `runtime.kind` 是同一套逻辑的另一半：跑的是真模型还是 Echo 回显，
    必须能从响应里看出来，否则前端只能把回显当成「模型答非所问」。
    """
    return {"status": "ok", "runtime": runtime_status(), **health_payload()}


def _profile_or_403(name: str | None) -> PermissionProfile:
    """解析请求里的档位；未开放/不存在一律 403，不静默降级。

    静默降级到默认档会让「我明明选了放开档」变成「怎么还是不行」，更难排查。
    """
    try:
        return resolve(name)
    except UnknownProfile:
        raise HTTPException(status_code=403, detail="permission_not_available") from None


def _registry(request: Request) -> SessionRegistry:
    """取会话注册表；首次访问时按当前 app.state.runtime 建工厂。

    注册表与「它基于哪个 runtime 实例」一起记住：测试/部署替换
    app.state.runtime 后必须重建，否则会一直用旧实例的工厂。
    """
    base = getattr(request.app.state, "runtime", None)
    registry = getattr(request.app.state, "sessions", None)
    if registry is None or getattr(request.app.state, "sessions_base", None) is not base:
        registry = SessionRegistry(session_runtime_factory(base))
        request.app.state.sessions = registry
        request.app.state.sessions_base = base
    return registry


def _sse_frame(payload: dict[str, Any]) -> str:
    """把一个 dict 包装成标准 SSE data 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _named_frame(name: str, payload: dict[str, Any]) -> str:
    """带 event 名的 SSE 帧（done / parked）。"""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _frames(
    source: AsyncIterator[dict[str, Any]], recorder: RunRecorder
) -> AsyncIterator[str]:
    """把运行时事件流翻译成 SSE 帧，同时喂给 recorder 落库。"""
    async for event in source:
        recorder.consume(event)
        yield _sse_frame(event)


def _load_attachments(ids: list[str]) -> list[Attachment]:
    """按 id 取回附件正文；读取失败一律降级成空列表，不阻断这一轮。"""
    if not ids:
        return []
    try:
        return AttachmentStore().load(ids)
    except Exception:
        return []


def _save_secret(name: str, value: str) -> None:
    """把解析阶段认出的凭据加密入库；保险箱不可用时只脱敏、不保存。"""
    try:
        get_vault().put(name, value, source="input")
    except Exception:
        return


def _prepare_input(user_input: str, attachments: list[Attachment]) -> PreparedInput | None:
    """解析输入；任何意外都退回 None（沿用原始输入），不让解析层阻断对话。"""
    try:
        return prepare(user_input, attachments=attachments, on_secret=_save_secret)
    except Exception:
        return None


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


# 结清挂起时最多允许多少轮追跑：模型被拒后通常会换个方案再试一次，
# 再被拒还继续要确认就是它自己的问题，不必无限追。
SETTLE_ROUNDS = 2


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


@app.get("/api/permissions")
def list_permissions() -> dict[str, Any]:
    """本部署开放的档位。默认档可能不在可用集里（配置写错），此时回落到首项。"""
    return {
        "default": default_profile().key,
        "available": [
            {
                "key": item.key,
                "label": item.label,
                "note": item.note,
                "fallback": item.fallback,
                "write": "write" in item.tools,
            }
            for item in available()
        ],
    }


@app.post("/run/stream")
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
    domain = os.environ.get("DEVOPS_AGENT_DOMAIN", "debug")
    model = os.environ.get("DEVOPS_AGENT_MODEL", "")
    recorder = RunRecorder(
        domain=domain,
        model=model,
        request_id=x_request_id or "",
        permission=profile.key,
    )
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
            # 会话快照要包含这一轮结束后的完整状态，刷新后重建才不会丢历史
            registry.save(req.session_id)
            entry.lock.release()

    # 三个响应头都是流式能被"真的是流"看到的前提：
    # - no-cache：避免中间层缓存整段响应
    # - keep-alive：保持长连接，别在帧间隙被关闭
    # - X-Accel-Buffering: no：关闭 nginx 等反向代理的响应缓冲，
    #   否则代理会把整段流攒到结束才下发，前端又变回一次性渲染
    return StreamingResponse(generate(), media_type="text/event-stream", headers=_SSE_HEADERS)


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


@app.get("/run/{run_id}/pending")
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


@app.post("/run/{run_id}/confirm")
async def confirm_run(run_id: int, req: ConfirmRequest, request: Request) -> StreamingResponse:
    """带着用户决策从挂起点继续跑同一个 run。

    幂等靠「条件更新」：只有把 parked 从 1 翻成 0 的那个请求才能继续，
    双击「允许一次」或两个标签页同时确认都只会执行一次。
    """
    row = _pending_row(run_id)
    _check_pending(row, req.session_id)
    profile = _profile_for_run(row)
    decisions = parse_decisions([item.model_dump() for item in req.results])
    if not decisions:
        raise HTTPException(status_code=400, detail="empty_results")
    registry = _registry(request)
    entry = registry.acquire(req.session_id, profile)
    if not callable(getattr(entry.runtime, "resume", None)):
        raise HTTPException(status_code=400, detail="runtime_not_resumable")
    if entry.lock.locked():
        raise HTTPException(status_code=409, detail="session_busy")
    with repo.connect() as conn:
        repo.init_db(conn)
        if not repo.unpark_run(conn, run_id, now=utc_now()):
            raise HTTPException(status_code=409, detail="already_resolved")
    recorder = RunRecorder(
        domain=os.environ.get("DEVOPS_AGENT_DOMAIN", "debug"),
        model=os.environ.get("DEVOPS_AGENT_MODEL", ""),
        permission=profile.key,
    )
    recorder.attach(run_id, req.session_id)
    await entry.lock.acquire()

    async def generate() -> AsyncIterator[str]:
        try:
            yield ": ping\n\n"
            source = entry.runtime.resume(decisions)  # type: ignore[attr-defined]
            async for frame in _stream_entry(entry, recorder, source):
                yield frame
        except Exception as exc:
            recorder.finish(status="failed", error=f"{type(exc).__name__}: {exc}")
            yield _sse_frame({"type": "RUN_ERROR", "error": str(exc)})
        finally:
            registry.save(req.session_id)
            entry.lock.release()

    return StreamingResponse(generate(), media_type="text/event-stream", headers=_SSE_HEADERS)
