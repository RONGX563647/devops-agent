"""工具 schema 导出 —— 给前端 Inspector 的「Schema」页签与上下文行使用。

前端排查工具调用失败时，只看到入参不够，还需要知道这个工具**声明**了哪些参数。
schema 的唯一来源是 Toolkit 自己（`get_tool_schemas()`），不在这里手写一份，
否则注册表改了、界面上的参数说明就会跟着过期。

按档位导出（而不只是只读四件套）：放开档实际挂了 Write/Edit，界面却只显示四个，
「模型动手前手里有什么」这张表就是错的。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from devops_agent.tools.catalog import build_readonly_toolkit, build_toolkit_for

# 工具 schema 与运行时共用同一个工作目录：Bash/Read 的相对路径基准取决于它，
# 两边不一致会让界面上的默认值看起来对、实际跑起来是另一个目录。
WORKDIR_ENV = "DEVOPS_AGENT_WORKDIR"


def schemas_workdir() -> str:
    """工具 schema 使用的工作目录，缺省为当前目录。"""
    return os.environ.get(WORKDIR_ENV, ".")


async def tool_schemas(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """导出指定工具集的 OpenAI 形状 schema；不传则取只读四件套。

    顺序沿用 `names`（档位里写死的 tuple），不做排序：这里导出的顺序必须与
    运行时真正下发给模型的顺序一致，否则「前缀指纹」对不上，缓存排查就失去意义。
    """
    workdir = schemas_workdir()
    toolkit = (
        build_readonly_toolkit(workdir) if names is None else build_toolkit_for(names, workdir)
    )
    return await toolkit.get_tool_schemas()


async def readonly_tool_schemas() -> list[dict[str, Any]]:
    """导出只读工具集的 schema（保持旧调用点可用）。"""
    return await tool_schemas()
