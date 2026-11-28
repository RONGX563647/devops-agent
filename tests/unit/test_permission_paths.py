"""权限工作目录：Docker 模式只登记工具路径，不登记宿主路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from devops_agent.governance.compile import WORKDIRS_ENV, compile_context
from devops_agent.governance.model import PROFILES


def test_docker_workdir_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKDIRS_ENV, raising=False)
    context = compile_context(PROFILES["workspace"], workdir="/workspace")
    assert "/workspace" in context.working_directories


def test_container_workdir_keeps_its_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """容器路径必须原样登记：不能长出盘符，也不能带上父目录跳转。

    回归点：`Path("/workspace").resolve()` 在 Windows 上会把路径绑到「当前盘符的
    根」，于是从 `C:` 启动登记 `C:\\workspace`、从 `E:` 启动登记 `E:\\workspace`。
    容器里没有这个路径，越界判定就随进程启动目录漂移。
    """
    monkeypatch.delenv(WORKDIRS_ENV, raising=False)
    dirs = compile_context(PROFILES["workspace"], workdir="/workspace/").working_directories
    assert list(dirs) == ["/workspace"]
    assert not Path(next(iter(dirs))).drive


def test_container_workdir_from_env_is_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 里写容器路径时同样原样登记，不被宿主语义改写。"""
    monkeypatch.setenv(WORKDIRS_ENV, "/data")
    dirs = compile_context(PROFILES["workspace"]).working_directories
    assert list(dirs) == ["/data"]
    assert not Path(next(iter(dirs))).drive


def test_host_workdir_is_still_resolved(tmp_path: Path) -> None:
    """宿主路径照旧 resolve：`..` 要被吃掉，符号链接要归一。"""
    dirs = compile_context(PROFILES["workspace"], workdir=tmp_path / "sub" / "..")
    assert list(dirs.working_directories) == [str(tmp_path.resolve())]


def test_host_workdir_is_not_registered_in_docker_mode(tmp_path: Path) -> None:
    """容器路径才是模型能写的路径；登记宿主目录会让 accept_edits 每次都判越界。"""
    assert (
        str(tmp_path)
        not in compile_context(PROFILES["workspace"], workdir="/workspace").working_directories
    )


def test_env_workdirs_still_merge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """显式 env 附加目录保持既有语义，与 docker 模式不冲突。"""
    monkeypatch.setenv(WORKDIRS_ENV, str(tmp_path))
    context = compile_context(PROFILES["workspace"], workdir="/workspace")
    assert str(tmp_path) in context.working_directories
    assert "/workspace" in context.working_directories
