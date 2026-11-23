"""工具能力 API —— 暴露已装配工具的 schema，供前端展示参数形状。

放在 tools 包而不是 observability：schema 来自框架装配区，观测层刻意不 import
agentscope（它只认普通 dict）。由 main.py 统一 include 两个 router。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from devops_agent.tools.schema import readonly_tool_schemas

router = APIRouter(prefix="/api", tags=["tools"])


@router.get("/tools/schema", response_model=list[dict[str, Any]])
async def list_tool_schemas() -> list[dict[str, Any]]:
    """返回每个工具的 name、description 与 parameters JSON Schema。"""
    return await readonly_tool_schemas()
