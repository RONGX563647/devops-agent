"""工具目录与执行面的工厂单元测试。"""

import asyncio
import os
from pathlib import Path

from agentscope.tool import FunctionTool, ToolGroup, Toolkit
from agentscope.workspace import DockerWorkspace, LocalWorkspace

from devops_agent.executor.boundary import build_docker_workspace, build_local_workspace
from devops_agent.tools.catalog import (
    ALL_TOOL_NAMES,
    DEFAULT_GROUPS,
    PLATFORM_TOOL_NAMES,
    build_full_toolkit,
    build_readonly_toolkit,
    build_toolkit,
    shell_name,
)


def _names(toolkit: Toolkit) -> set[str]:
    """取工具名集合（get_tool_schemas 是 async，返回 OpenAI 风格的 function 描述）。"""
    schemas = asyncio.run(toolkit.get_tool_schemas())
    return {schema["function"]["name"] for schema in schemas}


def _tool() -> FunctionTool:
    def echo(x: int) -> int:
        return x

    return FunctionTool(func=echo, name="echo", description="echo an int", is_read_only=True)


def test_build_toolkit_empty() -> None:
    assert isinstance(build_toolkit(), Toolkit)


def test_build_toolkit_with_tools() -> None:
    assert isinstance(build_toolkit([_tool()]), Toolkit)


def test_build_readonly_toolkit_exposes_read_tools_only(tmp_path: Path) -> None:
    """只读工具集必须含 shell/read/glob/grep，且不得含 Edit/Write（写工具属写入组）。"""
    names = _names(build_readonly_toolkit(tmp_path))
    assert {"Bash", "Read", "Glob", "Grep"} <= names
    assert "Edit" not in names
    assert "Write" not in names


def test_build_readonly_toolkit_anchors_shell_at_workdir(tmp_path: Path) -> None:
    """shell 的 cwd 必须锁定在传入的工作目录，否则相对路径会落到进程 cwd。"""
    shell = asyncio.run(build_readonly_toolkit(tmp_path).get_tool("Bash"))
    assert shell is not None
    assert str(tmp_path.resolve()) == str(shell._cwd)


def test_shell_name_is_non_empty() -> None:
    assert shell_name().strip()


def test_full_toolkit_groups(tmp_path: Path) -> None:
    """全量工具按组注册：write / task 组存在，basic 工具直接注册。"""
    toolkit = build_full_toolkit(tmp_path)
    names = {group.name for group in toolkit.tool_groups}
    assert {"write", "task"} <= names
    assert "reset_tools" in _names(toolkit)
    assert isinstance(toolkit.tool_groups[0], ToolGroup)


def test_platform_tools_follow_platform(tmp_path: Path) -> None:
    """PowerShell 没有只读命令解析，只在 Windows 注册。"""
    groups = {group.name for group in build_full_toolkit(tmp_path).tool_groups}
    if os.name == "nt":
        assert PLATFORM_TOOL_NAMES == ("powershell",)
        assert "platform" in groups
        assert "platform" in DEFAULT_GROUPS
        assert "powershell" in ALL_TOOL_NAMES
    else:
        assert PLATFORM_TOOL_NAMES == ()
        assert "platform" not in groups
        assert "platform" not in DEFAULT_GROUPS
        assert "powershell" not in ALL_TOOL_NAMES


def test_build_local_workspace() -> None:
    ws = build_local_workspace("./workspace/test", skill_paths=["./skills"])
    assert isinstance(ws, LocalWorkspace)


def test_build_docker_workspace() -> None:
    ws = build_docker_workspace(base_image="python:3.11-slim", env={"K": "V"})
    assert isinstance(ws, DockerWorkspace)
