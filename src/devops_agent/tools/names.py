"""工具名的唯一事实来源（零依赖）。

档位（`governance/model.py`）按名字选工具，装配（`tools/catalog.py`）按名字建
工具。两件事必须看到同一份列表，否则「放开档少了写工具」会表现成「权限切了但
模型还是不写文件」。

单独成文件是为了切断 `governance` → `tools.catalog` → `agentscope` 的间接依赖：
这里只有两个元组，任何层都可以 import。
"""

from __future__ import annotations

READONLY_TOOL_NAMES: tuple[str, ...] = ("bash", "read", "glob", "grep")
WRITE_TOOL_NAMES: tuple[str, ...] = ("write", "edit")
