"""应用入口：装配 AgentRuntime 并启动 FastAPI 网关。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from devops_agent.env import load_env
from devops_agent.observability.api import router as observability_router
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.agent import build_agentscope_runtime
from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime


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
app.include_router(observability_router)


class RunRequest(BaseModel):
    session_id: str
    user_input: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
async def run(req: RunRequest, request: Request) -> dict[str, str]:
    runtime: AgentRuntime = request.app.state.runtime
    result = await runtime.run(req.session_id, req.user_input)
    return {"result": result}


def _sse_frame(payload: dict[str, Any]) -> str:
    """把一个 dict 包装成标准 SSE data 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/run/stream")
async def run_stream(
    req: RunRequest,
    request: Request,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> StreamingResponse:
    """SSE 流式端点：实时推送事件，并同步把事件落库为 run + spans。

    - 每个事件一个 `data: {...}` 帧，前端可边收边渲染；
    - 结束时发 `event: done` 帧带 run_id 与汇总；
    - X-Request-ID 提供幂等键，重试不会写入重复 run。
    """
    runtime: AgentRuntime = request.app.state.runtime
    # domain/model 由部署环境注入，观测数据才能反映真实运行上下文；
    # 缺省保持 debug/空串，与 Echo 自检及既有测试兼容。
    domain = os.environ.get("DEVOPS_AGENT_DOMAIN", "debug")
    model = os.environ.get("DEVOPS_AGENT_MODEL", "")
    recorder = RunRecorder(domain=domain, model=model, request_id=x_request_id or "")

    async def generate() -> AsyncIterator[str]:
        recorder.start(req.session_id, req.user_input)
        try:
            async for event in runtime.stream(req.session_id, req.user_input):
                recorder.consume(event)
                yield _sse_frame(event)
        except Exception as exc:  # 异常也要落库，记录失败
            recorder.finish(status="failed")
            yield _sse_frame({"type": "RUN_ERROR", "error": str(exc)})
            return
        run_id = recorder.finish()
        in_tok, out_tok = recorder.token_usage
        done = {
            "type": "RUN_DONE",
            "run_id": run_id,
            "status": "success",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "text": recorder.final_text,
        }
        yield f"event: done\ndata: {json.dumps(done, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
