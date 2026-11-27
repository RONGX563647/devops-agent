"""执行目标 —— 一次装配所需的全部执行面信息（双路径视图的唯一来源）。

`host_workdir` 是挂载 / 审计 / 文件归属的真实来源；`tool_workdir` 是模型与工具
看见的路径。local 模式两者相同；docker 模式宿主目录挂在 `/workspace`，模型只
能写容器内路径，而权限与审计仍以宿主路径为准。opensandbox 没有 bind mount：
沙箱内 `/workspace` 才是文件唯一所在，宿主目录降级为稳定键与审计标签。

这两条视图必须由同一个对象给出，否则会出现「提示词说 /workspace、权限只认宿主
目录」这种看起来对、文件就是找不到的偏差。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentscope.tool import BackendBase
from agentscope.workspace import DockerWorkspace, OpenSandboxWorkspace, WorkspaceBase

from devops_agent.tools.catalog import shell_name

# 容器 / 沙箱内的工作目录固定为 /workspace；shell 固定 /bin/sh。
CONTAINER_WORKDIR = "/workspace"
CONTAINER_SHELL = "/bin/sh"


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """一次装配实际生效的执行面。

    Attributes:
        mode: 实际生效模式（不是请求模式）——`auto` 回落时这里是 `local`。
        host_workdir: 挂载 / 审计使用的宿主目录；opensandbox 下仅作稳定键。
        tool_workdir: 模型与工具看到的路径（远程后端恒为 `/workspace`）。
        backend: 工具执行原语，来自已初始化的 workspace。
        shell: 系统提示里告知模型的 shell 语法。
        workspace: 已初始化的 workspace 句柄（关闭时用）。
        degraded_reason: 回落本机时的原因；正常为空串。
        sandbox_id: 远程沙箱实例标识（opensandbox 才有；未知时为空串）。
        init_ms: `ensure()` 创建 / 初始化耗时，用于验证预配置镜像的收益。
    """

    mode: Literal["local", "docker", "opensandbox"]
    host_workdir: Path
    tool_workdir: str
    backend: BackendBase
    shell: str
    workspace: WorkspaceBase | None = None
    degraded_reason: str = ""
    sandbox_id: str = ""
    init_ms: int = 0

    @property
    def uses_shared_backend(self) -> bool:
        """docker / opensandbox 共用无状态 backend；local 逐工具新建。"""
        return self.mode != "local"

    @property
    def is_docker(self) -> bool:
        """是否运行在 Docker 容器内（保留给只关心 docker 的既有调用）。"""
        return self.mode == "docker"

    def backend_factory(self) -> BackendBase:
        """返回工具共享的 backend。

        远程后端的 backend 无会话状态，六个工具共用同一实例即可；local 模式不走
        这里（保持既有 `_WorkdirBackend` 逐工具实例的语义）。
        """
        return self.backend


def build_target(
    workspace: WorkspaceBase,
    host_workdir: Path,
    degraded_reason: str = "",
    init_ms: int = 0,
) -> ExecutionTarget:
    """按 workspace 真实类型生成执行目标（不按请求模式猜）。

    `OpenSandboxWorkspace` 不是 `DockerWorkspace` 的子类
    （MRO：OpenSandboxWorkspace → SandboxedWorkspaceBase → WorkspaceBase），
    因此必须先判 opensandbox，否则会被误判为 local。
    """
    if isinstance(workspace, OpenSandboxWorkspace):
        return ExecutionTarget(
            mode="opensandbox",
            host_workdir=host_workdir,
            tool_workdir=CONTAINER_WORKDIR,
            backend=workspace.get_backend(),
            shell=CONTAINER_SHELL,
            workspace=workspace,
            degraded_reason=degraded_reason,
            sandbox_id=workspace.sandbox_id or "",
            init_ms=init_ms,
        )
    if isinstance(workspace, DockerWorkspace):
        return ExecutionTarget(
            mode="docker",
            host_workdir=host_workdir,
            tool_workdir=CONTAINER_WORKDIR,
            backend=workspace.get_backend(),
            shell=CONTAINER_SHELL,
            workspace=workspace,
            degraded_reason=degraded_reason,
            init_ms=init_ms,
        )
    return ExecutionTarget(
        mode="local",
        host_workdir=host_workdir,
        tool_workdir=str(host_workdir),
        backend=workspace.get_backend(),
        shell=shell_name(),
        workspace=workspace,
        degraded_reason=degraded_reason,
        init_ms=init_ms,
    )


__all__ = [
    "CONTAINER_SHELL",
    "CONTAINER_WORKDIR",
    "ExecutionTarget",
    "build_target",
]
