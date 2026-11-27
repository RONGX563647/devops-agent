"""执行目标：local/docker 的路径与 shell 映射。"""

from __future__ import annotations

from pathlib import Path

from agentscope.tool import LocalBackend

from devops_agent.executor.boundary import build_docker_workspace, build_local_workspace
from devops_agent.executor.target import CONTAINER_SHELL, CONTAINER_WORKDIR, build_target


def _fake_docker(host: Path):
    """构造一个未真正启动的 DockerWorkspace，并塞入可用 backend。

    `DockerWorkspace` 的 `_backend` 由 `initialize()` 设置；单测不触 Docker，
    因此直接注入一个本地 backend 只为让 `get_backend()` 可用。
    """
    workspace = build_docker_workspace(host_workdir=str(host))
    workspace._backend = LocalBackend()  # noqa: SLF001 - 测试替身
    return workspace


def test_local_target_maps_host_path(tmp_path: Path) -> None:
    workspace = build_local_workspace(str(tmp_path))
    target = build_target(workspace, tmp_path)

    assert target.mode == "local"
    assert target.tool_workdir == str(tmp_path)
    assert target.is_docker is False
    assert target.shell
    assert target.degraded_reason == ""


def test_docker_target_maps_container_path(tmp_path: Path) -> None:
    workspace = _fake_docker(tmp_path)
    target = build_target(workspace, tmp_path, degraded_reason="")

    assert target.mode == "docker"
    assert target.tool_workdir == CONTAINER_WORKDIR
    assert target.shell == CONTAINER_SHELL
    assert target.is_docker is True
    assert target.host_workdir == tmp_path
    # docker 模式六个工具共享同一个 backend
    assert target.backend_factory() is target.backend


def test_degraded_target_keeps_reason(tmp_path: Path) -> None:
    workspace = build_local_workspace(str(tmp_path))
    target = build_target(workspace, tmp_path, degraded_reason="docker_unavailable: no daemon")

    assert target.mode == "local"
    assert "docker_unavailable" in target.degraded_reason
