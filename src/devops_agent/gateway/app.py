"""FastAPI 应用装配：实例、路由挂载与启动期告警。

本模块最后装配：各路由模块不 import 它，依赖方向始终是「路由 → app」。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, FastAPI

from devops_agent.env import load_env
from devops_agent.gateway.confirm import router as confirm_router
from devops_agent.gateway.sessions import router as session_router
from devops_agent.gateway.stream import router as stream_router
from devops_agent.input.api import router as attachments_router
from devops_agent.model_api import router as model_config_router
from devops_agent.observability.api import router as observability_router
from devops_agent.openjudge.api import router as openjudge_router
from devops_agent.runtime.assembly import build_agentscope_runtime
from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime
from devops_agent.secrets.api import router as secrets_router
from devops_agent.tools.api import router as tools_router
from devops_agent.tools.context_api import router as context_router
from devops_agent.tools.preflight import health_payload, missing_tools

router = APIRouter()


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


@router.get("/health")
def health() -> dict[str, Any]:
    """存活 + 运行时模式 + 工具外部依赖就绪状态。

    缺依赖**不返回 5xx**：探针看到 5xx 会重启容器，而重启治不好缺少的可执行
    文件，只会把一次可诊断的降级放大成重启循环。所以状态放在 body 的
    `tools` 里，由调用方判断要不要报警。

    `runtime.kind` 是同一套逻辑的另一半：跑的是真模型还是 Echo 回显，
    必须能从响应里看出来，否则前端只能把回显当成「模型答非所问」。
    """
    return {"status": "ok", "runtime": runtime_status(), **health_payload()}


app.include_router(observability_router)
app.include_router(openjudge_router)
app.include_router(attachments_router)
app.include_router(secrets_router)
app.include_router(tools_router)
app.include_router(context_router)
# 模型参数三层可配（SPEC-0001）：部署默认 / 会话覆盖 / 只读凭据
app.include_router(model_config_router)
app.include_router(router)
app.include_router(stream_router)
app.include_router(confirm_router)
app.include_router(session_router)
