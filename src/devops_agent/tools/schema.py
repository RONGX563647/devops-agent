"""工具 schema 导出 —— 给前端 Inspector 的「Schema」页签与上下文行使用。

前端排查工具调用失败时，只看到入参不够，还需要知道这个工具**声明**了哪些参数。
schema 的唯一来源是 Toolkit 自己（`get_tool_schemas()`），不在这里手写一份，
否则注册表改了、界面上的参数说明就会跟着过期。

所有档位共用同一个 Toolkit，因此默认导出全量 schema；传 names 时按指定顺序导出，
供测试或特定页面裁剪。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from devops_agent.tools.catalog import activated_groups, build_full_toolkit, build_toolkit_for

# 工具 schema 与运行时共用同一个工作目录：Bash/Read 的相对路径基准取决于它，
# 两边不一致会让界面上的默认值看起来对、实际跑起来是另一个目录。
WORKDIR_ENV = "DEVOPS_AGENT_WORKDIR"


def schemas_workdir() -> str:
    """工具 schema 使用的工作目录，缺省为当前目录。"""
    return os.environ.get(WORKDIR_ENV, ".")


async def tool_schemas(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """导出 OpenAI 形状 schema；不传 names 时导出全量工具。

    顺序沿用装配顺序，不做排序：这里导出的顺序必须与运行时下发给模型的一致，
    否则「前缀指纹」对不上，缓存排查就失去意义。
    """
    workdir = schemas_workdir()
    if names is not None:
        return await build_toolkit_for(names, workdir).get_tool_schemas()
    toolkit = build_full_toolkit(workdir)
    # 默认激活的组要和运行时一致，否则界面上的工具清单会少掉写/任务组
    return await toolkit.get_tool_schemas(groups=list(activated_groups()))
