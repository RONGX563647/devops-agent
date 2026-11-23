"""应用入口：装配 AgentRuntime 并启动 FastAPI 网关。"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime

app = FastAPI(title="devops-agent")
runtime: AgentRuntime = EchoRuntime()


class RunRequest(BaseModel):
    session_id: str
    user_input: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
def run(req: RunRequest) -> dict[str, str]:
    return {"result": runtime.run(req.session_id, req.user_input)}
