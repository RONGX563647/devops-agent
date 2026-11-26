"""离线判定 —— 只问引擎，不执行命令。

给题库 lint 用：判断一条用例要求的命令在它声明的档位下是否真的可达。单独成模块
是因为它需要 import 框架的 `Bash` 构造一个真实工具实例，而 `model` / `presets`
必须保持零框架依赖。
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path

from agentscope.permission import PermissionEngine
from agentscope.tool import Bash

from devops_agent.governance.compile import build_engine
from devops_agent.governance.model import PermissionProfile


@lru_cache(maxsize=32)
def _engine_for(profile: PermissionProfile, workdir: str) -> PermissionEngine:
    """按 (档位, workdir) 复用引擎：lint 批量跑题时不必每次都重建上下文。"""
    return build_engine(profile, workdir=workdir)


def bash_decision(profile: PermissionProfile, command: str, workdir: str | Path) -> str:
    """该档位对一条 bash 命令的判定：`allow` / `ask` / `deny`。"""
    resolved = str(Path(workdir).expanduser().resolve())
    engine = _engine_for(profile, resolved)
    tool = Bash(cwd=resolved)
    decision = asyncio.run(engine.check_permission(tool, {"command": command}))
    return str(decision.behavior.value)
