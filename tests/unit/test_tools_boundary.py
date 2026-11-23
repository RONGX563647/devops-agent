"""工具目录与执行面的工厂单元测试。"""

from agentscope.tool import FunctionTool, ToolGroup, Toolkit
from agentscope.workspace import DockerWorkspace, LocalWorkspace

from devops_agent.executor.boundary import build_docker_workspace, build_local_workspace
from devops_agent.tools.catalog import build_toolkit, readonly_group, write_group


def _tool() -> FunctionTool:
    def echo(x: int) -> int:
        return x

    return FunctionTool(func=echo, name="echo", description="echo an int", is_read_only=True)


def test_build_toolkit_empty() -> None:
    assert isinstance(build_toolkit(), Toolkit)


def test_build_toolkit_with_tools() -> None:
    assert isinstance(build_toolkit([_tool()]), Toolkit)


def test_readonly_group() -> None:
    g = readonly_group([_tool()])
    assert isinstance(g, ToolGroup)
    assert g.name == "readonly"


def test_write_group() -> None:
    g = write_group([])
    assert isinstance(g, ToolGroup)
    assert g.name == "write"


def test_build_local_workspace() -> None:
    ws = build_local_workspace("./workspace/test", skill_paths=["./skills"])
    assert isinstance(ws, LocalWorkspace)


def test_build_docker_workspace() -> None:
    ws = build_docker_workspace(base_image="python:3.11-slim", env={"K": "V"})
    assert isinstance(ws, DockerWorkspace)
