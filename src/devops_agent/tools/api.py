"""工具能力 API —— 暴露已装配工具的 schema，以及外部依赖的就绪状态。

放在 tools 包而不是 observability：schema 来自框架装配区，观测层刻意不 import
agentscope（它只认普通 dict）。由 gateway/app.py 统一 include 两个 router。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from devops_agent.governance.presets import UnknownProfile, default_profile, resolve
from devops_agent.tools.preflight import readiness_payload
from devops_agent.tools.schema import tool_schemas

router = APIRouter(prefix="/api", tags=["tools"])


@router.get("/tools/schema", response_model=list[dict[str, Any]])
async def list_tool_schemas(profile: str | None = None) -> list[dict[str, Any]]:
    """按档位返回 name、description 与 parameters JSON Schema。

    profile 缺省取只读档：老调用方（Inspector 的 Schema 页签）行为不变；
    传了档位就必须和会话实际装配一致，否则「这个工具声明了什么」会答错。
    """
    try:
        resolved = resolve(profile or None)
    except UnknownProfile:
        resolved = default_profile()
    return await tool_schemas(resolved.tools)


@router.get("/tools/preflight")
async def tool_preflight() -> dict[str, Any]:
    """外部依赖预检：哪个工具缺什么可执行文件、怎么装。

    工具在 Schema 里"声明存在"和"真的能跑"是两件事：`Grep` 声明得好好的，
    机器上没有 `rg` 时每次调用都以 exit 127 结束。前端把这里的结果和 schema
    一起展示，就能解释「为什么这个工具一用就报错」。
    """
    return readiness_payload()
