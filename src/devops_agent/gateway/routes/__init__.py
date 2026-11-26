"""HTTP 面：4 个端点 + 请求体 + 依赖取用。

本包只做契约映射 —— 端点里出现的 `if` 分支或 `try` 越多，越可能放错了层（逻辑该在 `turn/`）。

`__init__` 汇总 4 个 router，装配根只需一次 import；请求体与依赖取用留在本包内，
因为它们只服务于 HTTP 边界。
"""

from __future__ import annotations

from devops_agent.gateway.routes.confirm import router as confirm_router
from devops_agent.gateway.routes.health import router as health_router
from devops_agent.gateway.routes.sessions import router as session_router
from devops_agent.gateway.routes.stream import router as stream_router

__all__ = ["confirm_router", "health_router", "session_router", "stream_router"]
