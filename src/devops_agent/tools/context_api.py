"""上下文 API —— 这一轮对话真正装了哪些东西。

轨迹里的「第 0 步」要回答的问题不是「模型说了什么」，而是「模型动手前手里有什么」：
系统提示词、可用工具清单、用户这条输入。三者都来自装配区，所以放在 tools 包里
和 schema 一起暴露；前端只负责展示，不自己拼一份假的上下文。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from devops_agent.runtime.agent import build_system_prompt, resolve_workdir
from devops_agent.tools.schema import readonly_tool_schemas

router = APIRouter(prefix="/api", tags=["context"])


@router.get("/context")
async def get_context() -> dict[str, Any]:
    """返回系统提示词原文、工具清单与工作目录。"""
    schemas = await readonly_tool_schemas()
    tools = [
        {
            "name": item.get("function", {}).get("name", ""),
            "description": item.get("function", {}).get("description", ""),
        }
        for item in schemas
    ]
    prompt = build_system_prompt(resolve_workdir())
    return {
        "system_prompt": prompt,
        "system_prompt_chars": len(prompt),
        "tools": tools,
        "workdir": str(resolve_workdir()),
    }
