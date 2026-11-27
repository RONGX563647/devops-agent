"""进程启动期：选运行时、自述状态、把缺失依赖喊出来。

与 HTTP 无关，但 `/health` 与启动告警都要用；放进 app.py 会形成反向 import。
"""

from __future__ import annotations

import logging
from typing import Any

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


def select_runtime(execution: Any | None = None) -> AgentRuntime:
    """按环境变量选择运行时：配齐凭据用真实模型，否则回退 Echo（便于自检/测试）。

    `execution` 是启动期固定的执行面（`ExecutionTarget`）；省略时走本机路径。
    """
    if Settings().has_credentials():
        return build_agentscope_runtime(execution=execution)
    return EchoRuntime()


def pick_runtime(execution: Any | None = None) -> AgentRuntime:
    """装配期的选择，带降级：装配失败退回 Echo 并喊出来。

    为什么不在 `select_runtime` 里降级：测试要能拿到真实异常。生产入口需要的是
    「配置库坏了也能起来服务，让人看到 /health 而不是 connection refused」——
    这与 `warn_missing_tools` 的取舍一致：降级是可用状态，只报警不拒绝启动。
    """
    try:
        return select_runtime(execution)
    except Exception:  # noqa: BLE001 - 任何装配失败都不该拦住进程
        logger.exception("运行时装配失败，降级为 Echo；对话不会调用模型")
        return EchoRuntime()


def load_settings() -> Settings:
    """装配期读取网关策略；失败时用代码默认值，同样不拦住启动。

    注意 `gateway` 域的 `hot=restart`：策略在装配期固定，所以坏库降级出来的
    默认值会一直用到进程重启 —— 这也是必须让 `/health` 说出「配置降级」的原因。
    """
    try:
        return Settings.load()
    except Exception:  # noqa: BLE001 - 策略读不到不该拦住进程
        logger.exception("网关策略加载失败，使用代码默认值")
        return Settings()


def _effective_model() -> str:
    """模型名以真正生效的解析结果为准。

    旧实现读 `Settings().model()`（只读 `DEVOPS_AGENT_MODEL`），而实际跑的是
    `model_settings.resolve()` 的结果 —— 界面在「模型抽屉里选了 A、环境变量写着 B」
    时会显示 B。自述字段说错比不说更糟，所以走同一个解析器。
    """
    try:
        from devops_agent.model_settings import resolve, stored_defaults, values_of

        values = values_of(resolve(stored=stored_defaults(), domain=Settings().domain()))
        return str(values.get("model") or "")
    except Exception:  # noqa: BLE001 - 自述失败不该让 /health 500
        logger.warning("读取生效模型名失败，回落环境变量", exc_info=True)
        return Settings().model()


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
    return {"kind": KIND_AGENTSCOPE, "model": _effective_model(), "code": "", "notice": ""}


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


def warn_memory_backend() -> None:
    """长期记忆请求了未接入的后端时喊一次。

    不喊的后果是「配置里写着 mem0、实际跑的是 local」，而这两者对用户是
    同一句「有记忆」，只有排障时才会发现根本没走向量检索。与缺依赖同理：
    只报警不拒绝启动，降级本身是可用状态。
    """
    from devops_agent.memory.runtime import memory_status

    status = memory_status()
    if status.get("reason"):
        logger.warning(
            "长期记忆降级：requested=%s effective=%s（%s）",
            status.get("requested", ""),
            status.get("effective", ""),
            status["reason"],
        )


def describe_sandbox(state: Any) -> dict[str, Any]:
    """`/health.sandbox` 的自述：请求模式、实际生效、降级原因与沙箱标识。

    降级不能静默：`required=true` 初始化失败时 `sandbox_error` 非空，这里如实
    标 `degraded=true` 并给出原因码；`auto` 回落本机时 reason 同样可见。
    `reused` / `bootstrap_skipped` 框架未直接暴露，管理器的 status 如实给 null
    或经验判断值，这里只透传，不补造。
    """
    manager = getattr(state, "sandbox_manager", None)
    execution = getattr(state, "execution", None)
    error = getattr(state, "sandbox_error", None)
    if manager is not None:
        payload: dict[str, Any] = dict(manager.status())
    else:
        mode = execution.mode if execution is not None else "local"
        payload = {
            "mode": mode,
            "effective": mode,
            "provider": mode,
            "degraded": False,
            "reason": "",
            "workspace_id": "",
            "sandbox_id": execution.sandbox_id if execution is not None else "",
            "image": "",
            "host_workdir": str(execution.host_workdir) if execution is not None else "",
            "tool_workdir": execution.tool_workdir if execution is not None else "",
            "containers": 0,
            "init_ms": execution.init_ms if execution is not None else 0,
            "reused": None,
            "bootstrap_skipped": None,
        }
    if error is not None:
        message = getattr(error, "message", "") or ""
        code = getattr(error, "code", "sandbox_unavailable")
        payload["degraded"] = True
        payload["reason"] = f"{code}: {message}".strip(": ")
    return payload


def warn_runtime(runtime: AgentRuntime) -> None:
    """降级到 Echo 只打日志，不拒绝启动 —— 自检和测试都要用 Echo。

    但绝不能一声不响：这条 warning 是排查「agent 突然变傻」时唯一的线索。
    """
    status = describe_runtime(runtime)
    if status["kind"] == KIND_ECHO:
        logger.warning("%s", status["notice"])
