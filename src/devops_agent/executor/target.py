"""执行目标 —— 一次装配所需的全部执行面信息（双路径视图的唯一来源）。

`host_workdir` 是挂载 / 审计 / 文件归属的真实来源；`tool_workdir` 是模型与工具
看见的路径。local 模式两者相同；docker 模式宿主目录挂在 `/workspace`，模型只
能写容器内路径，而权限与审计仍以宿主路径为准。

这两条视图必须由同一个对象给出，否则会出现「提示词说 /workspace、权限只认宿主
目录」这种看起来对、文件就是找不到的偏差。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentscope.tool import BackendBase
from agentscope.workspace import DockerWorkspace, WorkspaceBase

from devops_agent.tools.catalog import shell_name

# DockerWorkspace 的容器内工作目录固定为 /workspace；shell 固定 /bin/sh。
CONTAINER_WORKDIR = "/workspace"
CONTAINER_SHELL = "/bin/sh"


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """一次装配实际生效的执行面。

    Attributes:
        mode: 实际生效模式（不是请求模式）——`auto` 回落时这里是 `local`。
        host_workdir: 挂载 / 审计 / 文件归属使用的宿主目录。
        tool_workdir: 模型与工具看到的路径（docker 恒为 `/workspace`）。
        backend: 工具执行原语，来自已初始化的 workspace。
        shell: 系统提示里告知模型的 shell 语法。
        workspace: 已初始化的 workspace 句柄（关闭时用）。
        degraded_reason: 回落本机时的原因；正常为空串。
    """

    mode: Literal["local", "docker"]
    host_workdir: Path
    tool_workdir: str
    backend: BackendBase
    shell: str
    workspace: WorkspaceBase | None = None
    degraded_reason: str = ""

    @property
    def is_docker(self) -> bool:
        """是否运行在容器内（决定是否注入共享 backend）。"""
        return self.mode == "docker"

    def backend_factory(self) -> BackendBase:
        """返回工具共享的 backend。

        DockerBackend 无会话状态，六个工具共用同一实例即可；local 模式不走这里
        （保持既有 `_WorkdirBackend` 逐工具实例的语义）。
        """
        return self.backend


def build_target(
    workspace: WorkspaceBase,
    host_workdir: Path,
    degraded_reason: str = "",
) -> ExecutionTarget:
    """按 workspace 真实类型生成执行目标（不按请求模式猜）。"""
    if isinstance(workspace, DockerWorkspace):
        return ExecutionTarget(
            mode="docker",
            host_workdir=host_workdir,
            tool_workdir=CONTAINER_WORKDIR,
            backend=workspace.get_backend(),
            shell=CONTAINER_SHELL,
            workspace=workspace,
            degraded_reason=degraded_reason,
        )
    return ExecutionTarget(
        mode="local",
        host_workdir=host_workdir,
        tool_workdir=str(host_workdir),
        backend=workspace.get_backend(),
        shell=shell_name(),
        workspace=workspace,
        degraded_reason=degraded_reason,
    )


__all__ = [
    "CONTAINER_SHELL",
    "CONTAINER_WORKDIR",
    "ExecutionTarget",
    "build_target",
]
