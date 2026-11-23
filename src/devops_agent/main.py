"""应用入口：装配 AgentRuntime 并启动 FastAPI 网关。"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from pydantic import BaseModel

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
app.state.runtime = select_runtime()


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
