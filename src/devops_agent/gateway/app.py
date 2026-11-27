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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from devops_agent.config import load_sandbox
from devops_agent.config_store import schema
from devops_agent.config_store.api import router as config_router
from devops_agent.env import load_env
from devops_agent.executor.manager import SandboxManager, SandboxUnavailable
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
from devops_agent.gateway.routes.workspaces import router as workspace_router
from devops_agent.input.api import router as attachments_router
from devops_agent.model_api import router as model_config_router
from devops_agent.observability.api import router as observability_router
from devops_agent.openjudge.api import router as openjudge_router
from devops_agent.openjudge.api_edit import router as openjudge_edit_router
from devops_agent.openjudge.memory.api import router as memory_eval_router
from devops_agent.runtime.prompt import resolve_workdir
from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime
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
    injected = runtime

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """启动期初始化执行面，退出时关闭全部工作区。

        1) 未显式注入 runtime 时，读 sandbox 策略并 `ensure()` 执行目标；
        2) 初始化成功后同步装配真实运行时（此时才有事件循环执行 async 初始化）；
        3) `required=true` 且失败时记录错误，对话端点据此返回 503（不回落宿主）；
        4) 关闭阶段统一 `close_all()`，容器残留只记录。
        """
        manager: SandboxManager | None = None
        if injected is None:
            manager = SandboxManager(load_sandbox())
            application.state.sandbox_manager = manager
            try:
                target = await manager.ensure(resolve_workdir())
            except SandboxUnavailable as exc:
                application.state.sandbox_error = exc
                logger.error("沙箱初始化失败，对话端点将返回 503：%s", exc)
            else:
                application.state.execution = target
                application.state.runtime = pick_runtime(execution=target)
                if target.degraded_reason:
                    logger.warning("沙箱降级为 %s：%s", target.mode, target.degraded_reason)
                else:
                    logger.info(
                        "执行面就绪：mode=%s tool_workdir=%s", target.mode, target.tool_workdir
                    )
                warn_runtime(application.state.runtime)
        try:
            yield
        finally:
            if manager is not None:
                await manager.close_all()

    # 2) 运行时：显式注入优先；否则先放 Echo 占位，lifespan 内初始化执行面后再装配
    application = FastAPI(title=APP_TITLE, lifespan=lifespan)
    application.state.runtime = runtime if runtime is not None else EchoRuntime()
    application.state.execution = None
    application.state.sandbox_manager = None
    application.state.sandbox_error = None
    # 3) 策略与会话注册表：注册表懒建 —— 替换 runtime 后下一次请求自然用新工厂
    application.state.settings = load_settings()
    application.state.sessions = None
    application.state.sessions_base = None
    # 工作区服务可被测试/部署注入；未注入时按配置域懒建（enabled=false 时路由 404）
    application.state.workspace_service = None
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
        # 工作区（SPEC-0021）：默认关闭，enabled=false 时全部 404
        workspace_router,
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
        # runtime 未注入时此刻只是 Echo 占位，真正装配在 lifespan 内，告警也在那里
        if injected is not None:
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
