"""工具 schema 导出 —— 给前端 Inspector 的「Schema」页签使用。

前端排查工具调用失败时，只看到入参不够，还需要知道这个工具**声明**了哪些参数。
schema 的唯一来源是 Toolkit 自己（`get_tool_schemas()`），不在这里手写一份，
否则注册表改了、界面上的参数说明就会跟着过期。
"""

from __future__ import annotations

import os
from typing import Any

from devops_agent.tools.catalog import build_readonly_toolkit

# 工具 schema 与运行时共用同一个工作目录：Bash/Read 的相对路径基准取决于它，
# 两边不一致会让界面上的默认值看起来对、实际跑起来是另一个目录。
WORKDIR_ENV = "DEVOPS_AGENT_WORKDIR"


def schemas_workdir() -> str:
    """工具 schema 使用的工作目录，缺省为当前目录。"""
    return os.environ.get(WORKDIR_ENV, ".")


async def readonly_tool_schemas() -> list[dict[str, Any]]:
    """导出只读工具集的 OpenAI 形状 schema（name / description / parameters）。"""
    toolkit = build_readonly_toolkit(schemas_workdir())
    return await toolkit.get_tool_schemas()
