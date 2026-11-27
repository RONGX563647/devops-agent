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
