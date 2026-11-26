"""进程启动期：选运行时、自述状态、把缺失依赖喊出来。

与 HTTP 无关，但 `/health` 与启动告警都要用；放进 app.py 会形成反向 import。
"""

from __future__ import annotations

import logging

from devops_agent.gateway.settings import Settings
from devops_agent.runtime.assembly import build_agentscope_runtime
from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime
from devops_agent.tools.preflight import missing_tools

logger = logging.getLogger(__name__)

# 运行时种类（`/health` 的 `runtime.kind`）。前端据此显示「真模型 / 回显」横幅。
KIND_ECHO = "echo"
KIND_AGENTSCOPE = "agentscope"
# 未配凭据的提示码：前端按 code 出文案；`notice` 是过渡期的原文案
CODE_NO_CREDENTIALS = "no_model_credentials"
NOTICE_NO_CREDENTIALS = "未配置模型凭据（DEVOPS_AGENT_API_KEY），当前回复只是回显输入，不会调用模型"


def select_runtime() -> AgentRuntime:
    """按环境变量选择运行时：配齐凭据用真实模型，否则回退 Echo（便于自检/测试）。"""
    if Settings().has_credentials():
        return build_agentscope_runtime()
    return EchoRuntime()


def describe_runtime(runtime: AgentRuntime) -> dict[str, str]:
    """当前运行时的自述，供 `/health` 与前端横幅使用。

    为什么必须暴露：Echo 兜底的回复是 `[<session>] <输入>`，看起来「像在回答
    但答非所问」。没有这个字段时，「没配 key」和「agent 坏了」在界面上完全无法区分，
    用户只能反复重试（实测曾因此产生同一条输入的两个 run）。
    """
    if isinstance(runtime, EchoRuntime):
        return {
            "kind": KIND_ECHO,
            "model": "",
            "code": CODE_NO_CREDENTIALS,
            "notice": NOTICE_NO_CREDENTIALS,
        }
    return {"kind": KIND_AGENTSCOPE, "model": Settings().model(), "code": "", "notice": ""}


def warn_missing_tools() -> None:
    """启动时把缺失的外部依赖喊出来。

    只在进程启动说一次，而不是等每次调用失败才说：模型看到的是工具报错，
    运维看到的是这条日志，两边说的是同一件事，总得有人先把它讲清楚。
    不要做成 fail-fast —— 缺 `rg` 只影响一个工具，没必要拒绝启动。
    """
    missing = missing_tools()
    if missing:
        logger.warning(
            "外部依赖缺失，以下工具会持续失败：%s；安装方式见 GET /api/tools/preflight",
            ", ".join(missing),
        )


def warn_runtime(runtime: AgentRuntime) -> None:
    """降级到 Echo 只打日志，不拒绝启动 —— 自检和测试都要用 Echo。

    但绝不能一声不响：这条 warning 是排查「agent 突然变傻」时唯一的线索。
    """
    status = describe_runtime(runtime)
    if status["kind"] == KIND_ECHO:
        logger.warning("%s", status["notice"])
