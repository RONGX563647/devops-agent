"""工具能力 API —— 暴露已装配工具的 schema，供前端展示参数形状。

放在 tools 包而不是 observability：schema 来自框架装配区，观测层刻意不 import
agentscope（它只认普通 dict）。由 main.py 统一 include 两个 router。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from devops_agent.governance.profiles import UnknownProfile, resolve
from devops_agent.tools.schema import tool_schemas

router = APIRouter(prefix="/api", tags=["tools"])


@router.get("/tools/schema", response_model=list[dict[str, Any]])
async def list_tool_schemas(profile: str | None = None) -> list[dict[str, Any]]:
    """返回全量工具的 name、description 与 parameters JSON Schema。

    profile 仍做校验（未开放档位回落到默认档），但所有档位装配的是同一个
    Toolkit，schema 不再随档位变化。
    """
    try:
        resolve(profile or None)
    except UnknownProfile:
        pass  # 未知档位不影响 schema：所有档位装配的是同一套工具
    return await tool_schemas()
