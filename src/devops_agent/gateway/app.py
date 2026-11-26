"""FastAPI 应用装配（composition root）—— 只做注入与挂载。

本模块最后装配：各路由模块不 import 它，依赖方向始终是「路由 → app」。

为什么有 `create_app()`：进程级单例（运行时、设置、会话注册表）应该由装配根注入，
而不是靠 import 期的模块副作用。之前测试只能 `app.state.runtime = ...` 再手工复位
三个 state 键，一旦漏复位就污染后续用例；现在测试可以 `create_app(runtime=...)`
拿一个干净实例，生产入口仍是模块级 `app = create_app()`。

三处刻意的顺序约束：
1. `load_env()` 只在这里调一次，且必须在选运行时之前 —— 否则凭据读不到；
2. 不把 `load_env()` 放进 `select_runtime()` 内部 —— 否则测试里 monkeypatch.delenv
   会被 .env 重新填回来；
3. 启动告警在挂完路由之后，失败不影响服务可用。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from devops_agent.env import load_env
from devops_agent.gateway import health as health_module
from devops_agent.gateway.bootstrap import (
    describe_runtime,
    select_runtime,
    warn_missing_tools,
    warn_runtime,
)
from devops_agent.gateway.confirm import router as confirm_router
from devops_agent.gateway.sessions import router as session_router
from devops_agent.gateway.settings import Settings
from devops_agent.gateway.stream import router as stream_router
from devops_agent.input.api import router as attachments_router
from devops_agent.model_api import router as model_config_router
from devops_agent.observability.api import router as observability_router
from devops_agent.openjudge.api import router as openjudge_router
from devops_agent.openjudge.api_edit import router as openjudge_edit_router
from devops_agent.runtime.runtime import AgentRuntime
from devops_agent.secrets.api import router as secrets_router
from devops_agent.tools.api import router as tools_router
from devops_agent.tools.context_api import router as context_router

APP_TITLE = "devops-agent"


def create_app(*, runtime: AgentRuntime | None = None) -> FastAPI:
    """装配一个应用实例。

    1) 加载 .env（进程内只生效一次；显式传 runtime 时跳过选择，但 env 仍要加载，
       因为观测库路径等配置也走 env）；
    2) 选定运行时并挂到 `app.state`（测试/部署可整体替换这个键）；
    3) 挂网关设置（策略来自 gateway.yaml）与会话注册表占位（懒建）；
    4) 挂路由：业务模块先、网关的 HTTP 面后；
    5) 启动期告警：缺外部工具、降级到 Echo。
    """
    # 1) .env 只加载一次，且早于任何「读 env 做决定」的代码
    load_env()
    # 2) 运行时：显式注入优先，否则按凭据选择（配了 key 走真模型，否则 Echo）
    application = FastAPI(title=APP_TITLE)
    application.state.runtime = runtime if runtime is not None else select_runtime()
    # 3) 策略与会话注册表：注册表懒建 —— 替换 runtime 后下一次请求自然用新工厂
    application.state.settings = Settings.load()
    application.state.sessions = None
    application.state.sessions_base = None
    # 4) 路由：外部能力模块在前，网关自己的 HTTP 面在后（/health 先于流式端点）
    for business_router in (
        observability_router,
        openjudge_router,
        openjudge_edit_router,
        attachments_router,
        secrets_router,
        tools_router,
        context_router,
        # 模型参数三层可配（SPEC-0001）：部署默认 / 会话覆盖 / 只读凭据
        model_config_router,
    ):
        application.include_router(business_router)
    application.include_router(health_module.router)
    application.include_router(stream_router)
    application.include_router(confirm_router)
    application.include_router(session_router)
    # 5) 启动告警：只喊一次，且不 fail-fast（缺 rg 不该拒绝启动）
    warn_missing_tools()
    warn_runtime(application.state.runtime)
    return application


app = create_app()


def runtime_status(runtime: AgentRuntime | None = None) -> dict[str, str]:
    """当前运行时的自述（兼容入口）。

    省略参数时读模块级 `app` 的运行时 —— 测试会替换 `app.state.runtime` 再断言这里。
    真正的实现只有 `bootstrap.describe_runtime` 一份，避免两处话术漂移。
    """
    current: Any = app.state.runtime if runtime is None else runtime
    return describe_runtime(current)
