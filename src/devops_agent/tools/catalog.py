"""工具目录 —— 用 FunctionTool + Toolkit + ToolGroup。

工具级隔离由 FunctionTool 的 is_read_only 承担，这是"只读会话不暴露写工具"的
第一道边界：只读会话只装配 read_only=True 的工具组。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentscope.tool import FunctionTool, ToolGroup, Toolkit


def build_toolkit(tools: Sequence[FunctionTool] | None = None) -> Toolkit:
    """装配一个空或带工具的 Toolkit。"""
    return Toolkit(list(tools) if tools else None)


def readonly_group(tools: Sequence[FunctionTool]) -> ToolGroup:
    """只读工具组：description 必填（非 basic 组的 API 约束）。"""
    return ToolGroup(name="readonly", description="只读工具集", tools=list(tools))


def write_group(tools: Sequence[FunctionTool]) -> ToolGroup:
    """写入工具组。"""
    return ToolGroup(name="write", description="写入工具集", tools=list(tools))
