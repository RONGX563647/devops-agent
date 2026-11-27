"""执行面 —— 用内置沙箱后端，不手搓 Docker。

LocalWorkspace / DockerWorkspace / OpenSandboxWorkspace 已内置沙箱隔离
（工作区含 data/skills/sessions）。五道边界由 workspace 后端 + PermissionContext
共同承担。

本模块只负责「把配置翻译成一个已构造的 workspace」；初始化、生命周期与模式
解析在 `executor/manager.py`。容器创建、镜像构建、MCP gateway 全部交给内置
workspace 类，这里不复制任何 Docker / 远端调度逻辑。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from agentscope.workspace import DockerWorkspace, LocalWorkspace, OpenSandboxWorkspace

# api_key 是凭据：只从环境变量读，不落配置库、不进 /health、不传给被测命令。
OSB_API_KEY_ENV = "DEVOPS_AGENT_SANDBOX_OSB_API_KEY"


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


def opensandbox_workspace_id(host_workdir: str) -> str:
    """把宿主工作目录映射成稳定的 `workspace_id`（决定沙箱复用）。

    `OpenSandboxWorkspace` 用 `workspace_id` 写进沙箱 metadata，下次 `initialize()`
    按它找回暂停的沙箱并 resume/connect。若每次新建随机 UUID，进程重启后就再也
    命中不了同一沙箱，「预配置 + 暂停复用」这条加速路径等于失效。
    """

    resolved = str(Path(host_workdir).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return f"devops-agent-{digest[:16]}"


def _network_policy(raw: dict[str, object]) -> Any:
    """把配置里的 dict 转成 SDK 的 `NetworkPolicy`。

    延迟 import：未安装 `opensandbox` 时本模块仍可 import（local/docker 不受影响）；
    真的要走 opensandbox 才会在 ensure 阶段失败，由 manager 归类。
    """
    if not raw:
        return None
    from opensandbox.models.sandboxes import NetworkPolicy

    return NetworkPolicy(**raw)


def build_opensandbox_workspace(
    *,
    workspace_id: str,
    image: str = "python:3.11-slim",
    domain: str = "",
    protocol: Literal["http", "https"] = "https",
    request_timeout_seconds: float = 60.0,
    timeout_seconds: int = 1800,
    gateway_port: int = 5600,
    env: dict[str, str] | None = None,
    extra_pip: Sequence[str] | None = None,
    resource: dict[str, str] | None = None,
    network_policy: dict[str, object] | None = None,
    entrypoint: Sequence[str] | None = None,
) -> OpenSandboxWorkspace:
    """远端执行面：由 OpenSandbox 服务托管沙箱。

    仓库不接触 docker.sock、不建容器、不做文件同步；镜像、网络策略与资源限额
    全部作为构造参数交给内置类。构造不会启动沙箱（需 `await initialize()`）。
    """
    return OpenSandboxWorkspace(
        workspace_id=workspace_id,
        image=image,
        api_key=os.environ.get(OSB_API_KEY_ENV, ""),
        domain=domain,
        protocol=protocol,
        request_timeout_seconds=request_timeout_seconds,
        timeout_seconds=timeout_seconds,
        gateway_port=gateway_port,
        env=dict(env) if env else None,
        extra_pip=list(extra_pip) if extra_pip else None,
        resource=dict(resource) if resource else None,
        network_policy=_network_policy(network_policy or {}),
        entrypoint=list(entrypoint) if entrypoint else None,
    )


__all__ = [
    "OSB_API_KEY_ENV",
    "build_docker_workspace",
    "build_local_workspace",
    "build_opensandbox_workspace",
    "opensandbox_workspace_id",
]
