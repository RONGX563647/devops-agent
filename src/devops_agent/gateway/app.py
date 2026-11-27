"""FastAPI 应用装配（composition root）—— 只做注入与挂载。

装配根最后执行：路由模块不 import 它，依赖方向始终是「路由 → app」。
进程级单例（运行时 / 设置 / 会话注册表）经 `create_app()` 注入，而非 import 期副作用，
测试因此能拿独立实例；生产入口仍是模块级 `app = create_app()`。

三处顺序约束：
1. `load_env()` 只调一次，且早于任何「读 env 做决定」的代码；
2. 不把它放进 `select_runtime()` —— 否则测试里 monkeypatch.delenv 会被 .env 填回；
3. 启动告警在挂完路由之后，失败不影响服务可用。

降级约定：运行时装配失败、网关策略读不到时**不拒绝启动**，退回 Echo / 代码默认并
打日志 —— 配置库坏了应该是「/health 显示降级」，不是「端口连不上」。唯一的例外是
`DEVOPS_AGENT_CONFIG_MODE` 拼错：那是静态配置错误，两种模式的语义完全不同，
必须立刻报错而不是猜一个。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from devops_agent.config_store import schema
from devops_agent.config_store.api import router as config_router
from devops_agent.env import load_env
from devops_agent.gateway.bootstrap import (
    describe_runtime,
    load_settings,
    pick_runtime,
    warn_memory_backend,
    warn_missing_tools,
    warn_runtime,
)

# 兼容再导出：测试与旧代码从 `gateway.app` 取 `select_runtime`。
from devops_agent.gateway.bootstrap import select_runtime as select_runtime
from devops_agent.gateway.routes import (
    confirm_router,
    health_router,
    session_router,
    stream_router,
)
from devops_agent.gateway.routes.memory import router as memory_router
from devops_agent.input.api import router as attachments_router
from devops_agent.model_api import router as model_config_router
from devops_agent.observability.api import router as observability_router
from devops_agent.openjudge.api import router as openjudge_router
from devops_agent.openjudge.api_edit import router as openjudge_edit_router
from devops_agent.openjudge.memory.api import router as memory_eval_router
from devops_agent.runtime.runtime import AgentRuntime
from devops_agent.secrets.api import router as secrets_router
from devops_agent.tools.api import router as tools_router
from devops_agent.tools.context_api import router as context_router

APP_TITLE = "devops-agent"

logger = logging.getLogger(__name__)


def create_app(*, runtime: AgentRuntime | None = None) -> FastAPI:
    """装配一个应用实例。

    1) 加载 .env（进程内只生效一次；显式传 runtime 时跳过选择，但 env 仍要加载，
       因为观测库路径等配置也走 env）；
    2) 选定运行时并挂到 `app.state`（测试/部署可整体替换这个键）；
    3) 挂网关设置（策略来自部署配置）与会话注册表占位（懒建）；
    4) 挂路由：业务模块先、网关的 HTTP 面后；
    5) 启动期告警：缺外部工具、降级到 Echo。
    """
    # 1) .env 只加载一次，且早于任何「读 env 做决定」的代码
    load_env()
    # 1.5) 配置模式是静态错误必须当场报：`dbb` 不该被当成 file 静默跑
    schema.mode()
    # 2) 运行时：显式注入优先，否则按凭据选择（配了 key 走真模型，否则 Echo）
    application = FastAPI(title=APP_TITLE)
    application.state.runtime = runtime if runtime is not None else pick_runtime()
    # 3) 策略与会话注册表：注册表懒建 —— 替换 runtime 后下一次请求自然用新工厂
    application.state.settings = load_settings()
    application.state.sessions = None
    application.state.sessions_base = None
    # 4) 路由：外部能力模块在前，网关自己的 HTTP 面在后（/health 先于流式端点）
    for business_router in (
        observability_router,
        openjudge_router,
        openjudge_edit_router,
        memory_eval_router,
        attachments_router,
        secrets_router,
        tools_router,
        context_router,
        # 模型参数三层可配（SPEC-0001）：部署默认 / 会话覆盖 / 只读凭据
        model_config_router,
        # 配置中心（ADR-0005）：health / input / memory / gateway 的部署旋钮
        config_router,
        memory_router,
    ):
        application.include_router(business_router)
    application.include_router(health_router)
    application.include_router(stream_router)
    application.include_router(confirm_router)
    application.include_router(session_router)
    # 5) 启动告警：只喊一次，且不 fail-fast（缺 rg 不该拒绝启动）
    try:
        warn_missing_tools()
        warn_memory_backend()
        warn_runtime(application.state.runtime)
    except Exception:  # noqa: BLE001 - 告警自身失败更不能拦住服务
        logger.warning("启动告警执行失败", exc_info=True)
    return application


app = create_app()


def runtime_status(runtime: AgentRuntime | None = None) -> dict[str, str]:
    """当前运行时的自述（兼容入口）。

    省略参数时读模块级 `app` 的运行时 —— 测试会替换 `app.state.runtime` 再断言这里。
    真正的实现只有 `bootstrap.describe_runtime` 一份，避免两处话术漂移。
    """
    current: Any = app.state.runtime if runtime is None else runtime
    return describe_runtime(current)
