"""执行面 —— 用内置沙箱后端，不手搓 Docker。

LocalWorkspace / DockerWorkspace 已内置沙箱隔离（工作区含 data/skills/sessions）。
五道边界由 workspace 后端 + PermissionContext 共同承担。
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
) -> DockerWorkspace:
    """服务器执行面：Docker 沙箱隔离。"""
    return DockerWorkspace(base_image=base_image, host_workdir=host_workdir, env=env)
