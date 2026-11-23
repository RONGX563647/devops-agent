"""工具目录 —— 用内置工具 + Toolkit + ToolGroup 装配能力。

所有档位装配**同一个** Toolkit：核心工具直接注册，写工具 / 任务工具 /
平台工具放进具名 ToolGroup，默认全部激活。能不能执行由 PermissionEngine
判定，而不是靠「工具不在列表里」。

这样做的好处是工具 schema 在档位之间字节一致：切档只改权限上下文与尾注，
不会让模型前缀缓存从工具段开始整体失效。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from agentscope.tool import (
    AskUser,
    Bash,
    Edit,
    FunctionTool,
    Glob,
    Grep,
    PowerShell,
    Read,
    TaskCreate,
    TaskGet,
    TaskList,
    TaskUpdate,
    ToolGroup,
    Toolkit,
    ToolMiddlewareBase,
    Write,
)
from agentscope.tool._base import ToolBase

# 工具名的唯一事实来源。顺序即下发给模型的顺序，禁止运行期重排。
CORE_TOOL_NAMES: tuple[str, ...] = ("bash", "read", "glob", "grep", "ask_user")
WRITE_TOOL_NAMES: tuple[str, ...] = ("write", "edit")
TASK_TOOL_NAMES: tuple[str, ...] = ("task_create", "task_get", "task_list", "task_update")
# PowerShell 没有只读命令解析，每条命令都会 ASK；只在 Windows 注册。
PLATFORM_TOOL_NAMES: tuple[str, ...] = ("powershell",) if os.name == "nt" else ()

ALL_TOOL_NAMES: tuple[str, ...] = (
    CORE_TOOL_NAMES + WRITE_TOOL_NAMES + TASK_TOOL_NAMES + PLATFORM_TOOL_NAMES
)

# 非 basic 组的默认激活集：全开。reset_tools 只能在这个集合内启停。
DEFAULT_GROUPS: tuple[str, ...] = ("write", "task") + (("platform",) if PLATFORM_TOOL_NAMES else ())

Middlewares = Sequence[ToolMiddlewareBase] | None


def _factories(cwd: str, middlewares: Middlewares) -> dict[str, Callable[[], ToolBase]]:
    """工具名 → 构造器。用惰性构造避免为没选中的工具白建 backend。

    middlewares 必须逐工具挂 —— 框架把它挂在 ToolBase 上，
    密钥注入就靠这一层，漏一个就有一条绕过注入的路。
    """
    mws = list(middlewares) if middlewares else None
    factories: dict[str, Callable[[], ToolBase]] = {
        "bash": lambda: Bash(cwd=cwd, middlewares=mws),
        "read": lambda: Read(middlewares=mws),
        "glob": lambda: Glob(middlewares=mws),
        "grep": lambda: Grep(middlewares=mws),
        "ask_user": lambda: AskUser(middlewares=mws),
        "write": lambda: Write(middlewares=mws),
        "edit": lambda: Edit(middlewares=mws),
        "task_create": lambda: TaskCreate(middlewares=mws),
        "task_get": lambda: TaskGet(middlewares=mws),
        "task_list": lambda: TaskList(middlewares=mws),
        "task_update": lambda: TaskUpdate(middlewares=mws),
    }
    if PLATFORM_TOOL_NAMES:
        factories["powershell"] = lambda: PowerShell(cwd=cwd, middlewares=mws)
    return factories


def _build(mapping: dict[str, Callable[[], ToolBase]], names: Sequence[str]) -> list[ToolBase]:
    unknown = [name for name in names if name not in mapping]
    if unknown:
        raise ValueError(f"未知工具: {', '.join(unknown)}")
    return [mapping[name]() for name in names]


def build_full_toolkit(
    workdir: str | os.PathLike[str],
    middlewares: Middlewares = None,
) -> Toolkit:
    """装配全量内置工具：核心直接注册，其余按组注册并默认激活。"""
    cwd = str(Path(workdir).resolve())
    factories = _factories(cwd, middlewares)

    groups = [
        ToolGroup(
            name="write",
            description="在工作区内创建、修改、删除文件。只在需要落盘时使用。",
            tools=_build(factories, WRITE_TOOL_NAMES),
        ),
        ToolGroup(
            name="task",
            description="维护多步骤任务清单；步骤较多时用它跟踪进度。",
            tools=_build(factories, TASK_TOOL_NAMES),
        ),
    ]
    if PLATFORM_TOOL_NAMES:
        groups.append(
            ToolGroup(
                name="platform",
                description="Windows PowerShell；每条命令都需要用户确认。",
                tools=_build(factories, PLATFORM_TOOL_NAMES),
            )
        )

    return Toolkit(
        tools=_build(factories, CORE_TOOL_NAMES),
        tool_groups=groups,
    )


def activated_groups() -> list[str]:
    """新会话的默认激活组：全开，保证工具 schema 稳定。"""
    return list(DEFAULT_GROUPS)


def build_toolkit_for(
    names: Sequence[str],
    workdir: str | os.PathLike[str],
    middlewares: Middlewares = None,
) -> Toolkit:
    """按名字装配工具集（schema 导出与测试用的低层入口）。

    未知名直接抛 ValueError —— 静默跳过会让「放开档少了写工具」这种问题
    表现成「权限切了但模型还是不改文件」，很难查。
    """
    cwd = str(Path(workdir).resolve())
    return Toolkit(tools=_build(_factories(cwd, middlewares), names))


def build_toolkit(tools: Sequence[FunctionTool] | None = None) -> Toolkit:
    """装配一个空或带工具的 Toolkit。"""
    return Toolkit(list(tools) if tools else None)


def build_readonly_toolkit(
    workdir: str | os.PathLike[str],
    middlewares: Middlewares = None,
) -> Toolkit:
    """装配只读核心工具集（schema 导出与只读测试用）。"""
    return build_toolkit_for(CORE_TOOL_NAMES[:4], workdir, middlewares)


def shell_name() -> str:
    """当前平台 shell 的可读名称，供系统提示告知模型该用哪种语法。"""
    return "cmd.exe (Windows)" if os.name == "nt" else "/bin/sh"
