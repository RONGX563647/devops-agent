"""应用入口：装配 AgentRuntime 并启动 FastAPI 网关。"""

from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel

from devops_agent.runtime.agent import build_agentscope_runtime
from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime


def select_runtime() -> AgentRuntime:
    """按环境变量选择运行时：配齐凭据用真实模型，否则回退 Echo（便于自检/测试）。"""
    if os.environ.get("DEVOPS_AGENT_API_KEY"):
        return build_agentscope_runtime()
    return EchoRuntime()


app = FastAPI(title="devops-agent")
runtime: AgentRuntime = select_runtime()


class RunRequest(BaseModel):
    session_id: str
    user_input: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
async def run(req: RunRequest) -> dict[str, str]:
    result = await runtime.run(req.session_id, req.user_input)
    return {"result": result}
