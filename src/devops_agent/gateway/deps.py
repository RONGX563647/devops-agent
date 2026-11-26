"""请求级依赖 —— 只负责「从应用状态里取到对象」，不含任何业务判断。

FastAPI 的依赖注入就是这一层的全部机制，不额外包一层容器：
`app.state` 上的三个键（runtime / settings / sessions）是装配根写下的，
这里的函数负责把它们取出来并给出明确的缺省行为。

取舍：`settings_of` 在缺省时回落 `Settings.load()`，而不是抛错 —— 测试会直接替换
`app.state.runtime` 来跑端点，那种场景下没装配 settings 也应能跑（用 yaml 默认值）。
"""

from __future__ import annotations

from fastapi import Request

from devops_agent.gateway.errors import ErrorCode, http_error
from devops_agent.gateway.settings import Settings
from devops_agent.governance.profiles import PermissionProfile, UnknownProfile, resolve
from devops_agent.runtime.sessions import SessionRegistry, registry_for


def profile_or_403(name: str | None) -> PermissionProfile:
    """解析请求里的档位；未开放或不存在一律 403，不静默降级。

    静默降级到默认档会让「我明明选了放开档」变成「怎么还是不行」，更难排查；
    也更危险 —— 客户端以为自己在放开档，实际被降成只读。
    """
    try:
        return resolve(name)
    except UnknownProfile:
        raise http_error(ErrorCode.PERMISSION_NOT_AVAILABLE) from None


def registry_of(request: Request) -> SessionRegistry:
    """取会话注册表（实现见 runtime/sessions.registry_for，模型配置端点共用同一份）。"""
    return registry_for(request)


def settings_of(request: Request) -> Settings:
    """取本进程的网关设置；未装配时回落到配置默认（测试替身友好）。"""
    found = getattr(request.app.state, "settings", None)
    return found if isinstance(found, Settings) else Settings.load()
