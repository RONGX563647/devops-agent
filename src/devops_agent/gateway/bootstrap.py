"""进程启动期：选运行时、自述状态、把缺失依赖喊出来。

与 HTTP 无关，但 `/health` 与启动告警都要用；放进 app.py 会形成反向 import。
"""

from __future__ import annotations

import logging

from devops_agent.gateway.settings import Settings
from devops_agent.model_catalog import DEFAULT_ID, get_catalog
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


def _configure_catalog() -> None:
    """装配期把模型目录注入参数解析层。

    失败只警告不拦启动：没有能力合并时窗口回落到静态 yaml 值，那是可用状态。
    但必须留痕 —— 「配了网关却仍按 128k 压缩」的唯一线索就是这条日志。
    """
    try:
        from devops_agent.model_catalog import configure

        configure()
    except Exception:  # noqa: BLE001 - 模型目录坏了不该拦住进程
        logger.exception("模型目录初始化失败，窗口回落到静态配置")


def select_runtime() -> AgentRuntime:
    """按环境变量选择运行时：配齐凭据用真实模型，否则回退 Echo（便于自检/测试）。"""
    _configure_catalog()
    if Settings().has_credentials():
        return build_agentscope_runtime()
    return EchoRuntime()


def pick_runtime() -> AgentRuntime:
    """装配期的选择，带降级：装配失败退回 Echo 并喊出来。

    为什么不在 `select_runtime` 里降级：测试要能拿到真实异常。生产入口需要的是
    「配置库坏了也能起来服务，让人看到 /health 而不是 connection refused」——
    这与 `warn_missing_tools` 的取舍一致：降级是可用状态，只报警不拒绝启动。
    """
    try:
        return select_runtime()
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


def _resolved_values() -> dict[str, object]:
    """当前生效的模型参数值（不抛：自述失败不该让 /health 500）。"""
    from devops_agent.model_settings import resolve, stored_defaults, values_of

    values = values_of(resolve(stored=stored_defaults(), domain=Settings().domain()))
    return dict(values)


def _effective_model() -> str:
    """模型标识以真正生效的解析结果为准，多网关时写成 `<provider>/<model>`。

    旧实现读 `Settings().model()`（只读 `DEVOPS_AGENT_MODEL`），而实际跑的是
    `model_settings.resolve()` 的结果 —— 界面在「模型抽屉里选了 A、环境变量写着 B」
    时会显示 B。自述字段说错比不说更糟，所以走同一个解析器。

    匿名 provider 仍只回裸模型名：`/health.runtime.model` 的形状不能因为
    新增一个键而变（A35 的等价性包含自述字段）。
    """
    try:
        values = _resolved_values()
        name = str(values.get("model") or "")
        provider = str(values.get("provider") or "")
        if provider and provider != DEFAULT_ID:
            return f"{provider}/{name}"
        return name
    except Exception:  # noqa: BLE001 - 自述失败不该让 /health 500
        logger.warning("读取生效模型名失败，回落环境变量", exc_info=True)
        return Settings().model()


def _as_int(value: object) -> int:
    """解析结果里的数字来自 yaml/env（int）或校验过的值，统一成 int 再自述。"""
    return int(value) if isinstance(value, (int, float)) else 0


def model_status() -> dict[str, object]:
    """`/health.model`：当前网关、协议、窗口，以及目录整体的健康。

    为什么要单列一段而不是塞进 `runtime`：`runtime.kind` 说的是「真模型还是回显」，
    这里说的是「用的是哪个网关的哪个模型、它的窗口从哪来」—— 两件事混在一起
    会让「模型答非所问」和「窗口配错了」看起来是同一个症状。
    """
    try:
        values = _resolved_values()
        catalog = get_catalog()
        registry = catalog.registry
        # 未显式选网关时，生效值是注册表的默认项 —— 不能写成常量 "default"：
        # 写了 providers.yaml 却没有 default 叫 default 时，这里会自述一个不存在的
        # id，而 `registry.get()` 会抛，/health 的 model 段就整块空了。
        provider = str(values.get("provider") or "") or registry.default_id
        spec = registry.get(provider)
        return {
            "provider": spec.id,
            "name": str(values.get("model") or ""),
            "kind": spec.kind,
            "context_size": _as_int(values.get("context_size")),
            "context_auto": bool(values.get("context_auto")),
            "catalog": catalog.status(),
        }
    except Exception:  # noqa: BLE001 - 目录读不到时仍要能说明「没读到」
        logger.warning("模型目录自述失败", exc_info=True)
        return {
            "provider": "",
            "name": "",
            "kind": "",
            "context_size": 0,
            "context_auto": False,
            "catalog": {},
        }


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


def warn_runtime(runtime: AgentRuntime) -> None:
    """降级到 Echo 只打日志，不拒绝启动 —— 自检和测试都要用 Echo。

    但绝不能一声不响：这条 warning 是排查「agent 突然变傻」时唯一的线索。
    """
    status = describe_runtime(runtime)
    if status["kind"] == KIND_ECHO:
        logger.warning("%s", status["notice"])
