"""工具目录 —— 用内置工具 + Toolkit + ToolGroup 装配能力。

工具级隔离由 `is_read_only` 与工具集合本身共同承担：只读会话只装配
shell/read/glob/grep 四个只读工具，`Edit` / `Write` 属写入组，不得进入
`build_readonly_toolkit` 的返回值。

为什么 shell 一律用内置 `Bash`（而不是按平台切 `PowerShell`）：
- `Bash` 会按命令判定只读（`ls` / `cat` / `git status` 直接放行），
  非只读命令才交给权限引擎；
- `PowerShell` 工具不区分只读命令，**每条命令都返回 ASK**。服务端没有
  人工确认通道，ASK 会退化成「发一个确认事件后结束」，表现为一轮对话
  静默中断而没有错误 —— 这正是修复前线上看到的现象；
- `Bash` 在 Windows 上通过 `cmd /c` 执行（Git for Windows 自带 ls/cat 等），
  在 POSIX 上通过 `/bin/sh -c` 执行，两端语义一致。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from agentscope.tool import (
    Bash,
    Edit,
    FunctionTool,
    Glob,
    Grep,
    Read,
    ToolGroup,
    Toolkit,
    ToolMiddlewareBase,
    Write,
)
from agentscope.tool._base import ToolBase

# 工具名的唯一事实来源：档位（governance/profiles.py）按名字选工具，
# 两边共用这两个元组，改工具集只改这里。
READONLY_TOOL_NAMES: tuple[str, ...] = ("bash", "read", "glob", "grep")
WRITE_TOOL_NAMES: tuple[str, ...] = ("write", "edit")

Middlewares = Sequence[ToolMiddlewareBase] | None


def _factories(cwd: str, middlewares: Middlewares) -> dict[str, Callable[[], ToolBase]]:
    """工具名 → 构造器。用惰性构造避免为没选中的工具白建 backend。

    middlewares 必须逐工具挂 —— 框架把它挂在 ToolBase 上，
    密钥注入就靠这一层，漏一个就有一条绕过注入的路。
    """
    mws = list(middlewares) if middlewares else None
    return {
        "bash": lambda: Bash(cwd=cwd, middlewares=mws),
        "read": lambda: Read(middlewares=mws),
        "glob": lambda: Glob(middlewares=mws),
        "grep": lambda: Grep(middlewares=mws),
        "write": lambda: Write(middlewares=mws),
        "edit": lambda: Edit(middlewares=mws),
    }


def build_toolkit_for(
    names: Sequence[str],
    workdir: str | os.PathLike[str],
    middlewares: Middlewares = None,
) -> Toolkit:
    """按名字装配工具集（档位切换的唯一入口）。

    未知名直接抛 ValueError —— 静默跳过会让「放开档少了写工具」这种问题
    表现成「权限切了但模型还是不改文件」，很难查。
    """
    cwd = str(Path(workdir).resolve())
    factories = _factories(cwd, middlewares)
    unknown = [name for name in names if name not in factories]
    if unknown:
        raise ValueError(f"未知工具: {', '.join(unknown)}")
    return Toolkit(tools=[factories[name]() for name in names])


def build_toolkit(tools: Sequence[FunctionTool] | None = None) -> Toolkit:
    """装配一个空或带工具的 Toolkit。"""
    return Toolkit(list(tools) if tools else None)


def build_readonly_toolkit(
    workdir: str | os.PathLike[str],
    middlewares: Middlewares = None,
) -> Toolkit:
    """装配内置只读工具集：shell + 读文件 + 路径查找 + 文本检索。

    只读会话的固定组合；写工具不得进入这里（要写就显式走 build_toolkit_for）。
    """
    return build_toolkit_for(READONLY_TOOL_NAMES, workdir, middlewares)


def shell_name() -> str:
    """当前平台 shell 的可读名称，供系统提示告知模型该用哪种语法。"""
    return "cmd.exe (Windows)" if os.name == "nt" else "/bin/sh"


def readonly_group(tools: Sequence[FunctionTool]) -> ToolGroup:
    """只读工具组：description 必填（非 basic 组的 API 约束）。"""
    return ToolGroup(name="readonly", description="只读工具集", tools=list(tools))


def write_group(tools: Sequence[FunctionTool]) -> ToolGroup:
    """写入工具组。"""
    return ToolGroup(name="write", description="写入工具集", tools=list(tools))
