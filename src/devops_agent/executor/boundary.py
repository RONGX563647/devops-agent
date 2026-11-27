"""执行面 —— 用内置沙箱后端，不手搓 Docker。

LocalWorkspace / DockerWorkspace 已内置沙箱隔离（工作区含 data/skills/sessions）。
五道边界由 workspace 后端 + PermissionContext 共同承担。

本模块只负责「把配置翻译成一个已构造的 workspace」；初始化、生命周期与模式
解析在 `executor/manager.py`。容器创建、镜像构建、MCP gateway 全部交给
`DockerWorkspace`，这里不复制任何 Docker 逻辑。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentscope.workspace import DockerWorkspace, LocalWorkspace


def build_local_workspace(
    workdir: str,
    skill_paths: Sequence[str] | None = None,
) -> LocalWorkspace:
    """本机执行面：直接作用于宿主机目录。"""
    return LocalWorkspace(workdir=workdir, skill_paths=list(skill_paths) if skill_paths else None)


def build_docker_workspace(
    base_image: str = "python:3.11-slim",
    host_workdir: str | None = None,
    env: dict[str, str] | None = None,
    *,
    extra_pip: Sequence[str] | None = None,
    gateway_port: int = 5600,
) -> DockerWorkspace:
    """服务器执行面：Docker 沙箱隔离。

    `host_workdir` 是 bind mount 的宿主来源；容器内路径固定为 `/workspace`。
    构造不会启动容器（需 `await initialize()`）。
    """
    return DockerWorkspace(
        base_image=base_image,
        host_workdir=host_workdir,
        env=env,
        extra_pip=list(extra_pip) if extra_pip else None,
        gateway_port=gateway_port,
    )
