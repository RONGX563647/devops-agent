"""工具目录 —— 用内置工具 + Toolkit + ToolGroup 装配能力。

工具级隔离由 `is_read_only` 与工具集合本身共同承担：只读会话只装配
shell/read/glob/grep 四个只读工具，`Edit` / `Write` 属写入组，不得进入
`build_readonly_toolkit` 的返回值。

`Grep` 依赖外部可执行文件 `rg`（ripgrep）：它通过 backend 直接 spawn 进程，
不走 shell，因此缺少 `rg` 时表现为 exit 127 而不是「命令不存在」。这份依赖
由 `tools/preflight.py` 探测并在 `/health` 与 `/api/tools/preflight` 暴露。

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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agentscope.tool import (
    BackendBase,
    Bash,
    Edit,
    FunctionTool,
    Glob,
    Grep,
    LocalBackend,
    Read,
    ToolGroup,
    Toolkit,
    ToolMiddlewareBase,
    Write,
)
from agentscope.tool._base import ToolBase

from devops_agent.tools.guard import build_guard
from devops_agent.tools.names import READONLY_TOOL_NAMES as READONLY_NAMES
from devops_agent.tools.names import WRITE_TOOL_NAMES as WRITE_NAMES

# 工具名的唯一事实来源是 tools/names.py（零依赖）：档位按名字选工具、
# 本模块按名字装配，两边共用同一份元组。这里 re-export 是为了既有 import 不破。
READONLY_TOOL_NAMES: tuple[str, ...] = READONLY_NAMES
WRITE_TOOL_NAMES: tuple[str, ...] = WRITE_NAMES

Middlewares = Sequence[ToolMiddlewareBase] | None
# 工具 backend 的工厂：省略时按 cwd 建 `_WorkdirBackend`（本机语义不变），
# Docker 模式传入「返回同一个 DockerBackend」的闭包。
BackendFactory = Callable[[], BackendBase]


def _constant(tool: ToolBase) -> Callable[[], ToolBase]:
    """把「已构造好的实例」包成工厂。用闭包而不是 lambda 默认参数：
    mypy 在循环里推断 `lambda tool=tool:` 的参数类型会失败。"""

    def make() -> ToolBase:
        return tool

    return make


class _WorkdirBackend(LocalBackend):
    """把 `getcwd()` 锚定到工作区的本地 backend。

    agentscope 2.0 的 `LocalBackend` 不再接受 `workdir`，`getcwd()` 直接返回
    服务进程的当前目录；而 Read/Glob/Grep 没有 `cwd` 参数，相对路径全靠它解析。
    不覆盖的话，评测里这三个工具会去搜部署目录而不是用例工作区，
    表现为「工作区明明有 app.py，Read 却说文件不存在」。
    """

    def __init__(self, workdir: str) -> None:
        # 用独立字段而不是 `_workdir`：基类把它声明成 `str | None`，
        # 复用会让 mypy 认为 getcwd() 可能返回 None，也可能碰到基类内部实现。
        self._cwd_root: str = workdir

    async def getcwd(self) -> str:
        return self._cwd_root


def _factories(
    cwd: str,
    middlewares: Middlewares,
    scope: str,
    extra: Mapping[str, ToolBase] | None = None,
    backend_factory: BackendFactory | None = None,
) -> dict[str, Callable[[], ToolBase]]:
    """工具名 → 构造器。用惰性构造避免为没选中的工具白建 backend。

    middlewares 必须逐工具挂 —— 框架把它挂在 ToolBase 上，
    密钥注入就靠这一层，漏一个就有一条绕过注入的路。

    backend 也必须逐工具挂，而且**必须锚定 workdir**：只有 `Bash` 有独立的
    `cwd` 参数，其余五个工具一律通过 `backend.getcwd()` 解析相对路径。
    不给 backend 时框架会新建 `LocalBackend()`，`getcwd()` 回落到
    **服务进程的当前目录**——那是部署目录，不是档位要操作的 workdir。
    表现是「Grep/Glob 不传 path 就搜错地方」，而且 bash 的 cd 与工具的
    相对路径各算各的，人很难把两件事联系起来。

    `extra` 是调用方已构造好的工具实例（目前只有 `web_search`）：它的预算要跨轮
    `reset()`，所以不能每次装配都 new 一个新的，必须由运行时持有同一个对象。
    """

    def backend() -> BackendBase:
        """取一个 backend：本机模式逐工具新建，容器模式共用一个无状态后端。

        本机模式每个工具独占实例，避免将来有状态时互相污染；Docker 模式下
        `DockerBackend` 本身无会话状态，共用同一实例即可。
        """
        if backend_factory is not None:
            return backend_factory()
        return _WorkdirBackend(cwd)

    # 守卫在最外层：先判路径，命中就短路，后面的密钥注入与工具执行都不跑。
    # 边界用 `cwd`（docker 模式即 /workspace），不是宿主目录。
    mws: list[ToolMiddlewareBase] = [build_guard(cwd, scope), *(middlewares or [])]
    factories: dict[str, Callable[[], ToolBase]] = {
        "bash": lambda: Bash(cwd=cwd, middlewares=mws, backend=backend()),
        "read": lambda: Read(middlewares=mws, backend=backend()),
        "glob": lambda: Glob(middlewares=mws, backend=backend()),
        "grep": lambda: Grep(middlewares=mws, backend=backend()),
        "write": lambda: Write(middlewares=mws, backend=backend()),
        "edit": lambda: Edit(middlewares=mws, backend=backend()),
    }
    for name, tool in (extra or {}).items():
        factories[name] = _constant(tool)
    return factories


def build_toolkit_for(
    names: Sequence[str],
    workdir: str | os.PathLike[str],
    middlewares: Middlewares = None,
    *,
    scope: str = "workspace",
    extra: Mapping[str, ToolBase] | None = None,
    backend_factory: BackendFactory | None = None,
    tool_cwd: str | None = None,
) -> Toolkit:
    """按名字装配工具集（档位切换的唯一入口）。

    未知名直接抛 ValueError —— 静默跳过会让「放开档少了写工具」这种问题
    表现成「权限切了但模型还是不改文件」，很难查。
    `scope` 决定敏感路径守卫是否把「工作区内」当硬边界（只有 readonly 档是）。

    `tool_cwd` 覆盖工具与 guard 使用的路径（docker 模式传 `/workspace`）；
    `backend_factory` 覆盖 backend 来源（docker 模式传入共享的 DockerBackend）。
    两者省略时行为与本机模式逐字节一致。
    """
    cwd = tool_cwd or str(Path(workdir).resolve())
    factories = _factories(cwd, middlewares, scope, extra, backend_factory)
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
    *,
    scope: str = "readonly",
) -> Toolkit:
    """装配内置只读工具集：shell + 读文件 + 路径查找 + 文本检索。

    只读会话的固定组合；写工具不得进入这里（要写就显式走 build_toolkit_for）。
    """
    return build_toolkit_for(READONLY_TOOL_NAMES, workdir, middlewares, scope=scope)


def shell_name() -> str:
    """当前平台 shell 的可读名称，供系统提示告知模型该用哪种语法。"""
    return "cmd.exe (Windows)" if os.name == "nt" else "/bin/sh"


def readonly_group(tools: Sequence[FunctionTool]) -> ToolGroup:
    """只读工具组：description 必填（非 basic 组的 API 约束）。"""
    return ToolGroup(name="readonly", description="只读工具集", tools=list(tools))


def write_group(tools: Sequence[FunctionTool]) -> ToolGroup:
    """写入工具组。"""
    return ToolGroup(name="write", description="写入工具集", tools=list(tools))
