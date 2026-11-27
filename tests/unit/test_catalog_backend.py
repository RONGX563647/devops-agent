"""工具 backend 注入与 tool_cwd 透传（Docker 执行面的接线点）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentscope.tool import LocalBackend, Toolkit

from devops_agent.tools.catalog import build_toolkit_for

_ALL = ["bash", "read", "glob", "grep", "write", "edit"]
_TOOL_NAMES = ("Bash", "Read", "Glob", "Grep", "Write", "Edit")


def _backends(toolkit: Toolkit) -> dict[str, object]:
    async def collect() -> dict[str, object]:
        result: dict[str, object] = {}
        for name in _TOOL_NAMES:
            tool = await toolkit.get_tool(name)
            assert tool is not None
            result[name] = tool._backend
        return result

    return asyncio.run(collect())


def test_injected_backend_is_shared(tmp_path: Path) -> None:
    """Docker 模式：六个工具共用一个无状态 backend。"""
    shared = LocalBackend()
    toolkit = build_toolkit_for(
        _ALL, tmp_path, backend_factory=lambda: shared, tool_cwd="/workspace"
    )
    backends = _backends(toolkit)
    assert all(item is shared for item in backends.values())


def test_tool_cwd_anchors_bash(tmp_path: Path) -> None:
    toolkit = build_toolkit_for(["bash"], tmp_path, tool_cwd="/workspace")
    shell = asyncio.run(toolkit.get_tool("Bash"))
    assert shell is not None
    assert shell._cwd == "/workspace"


def test_default_backends_stay_local_and_distinct(tmp_path: Path) -> None:
    """不注入时保持既有语义：每个工具独占一个锚在工作目录的 backend。"""
    toolkit = build_toolkit_for(["grep", "glob"], tmp_path)

    async def inspect() -> tuple[str, str, object, object]:
        grep = await toolkit.get_tool("Grep")
        glob = await toolkit.get_tool("Glob")
        assert grep is not None and glob is not None
        return (
            await grep._backend.getcwd(),
            await glob._backend.getcwd(),
            grep._backend,
            glob._backend,
        )

    grep_cwd, glob_cwd, grep_backend, glob_backend = asyncio.run(inspect())
    assert grep_cwd == glob_cwd == str(tmp_path.resolve())
    assert grep_backend is not glob_backend
