"""黄金镜像 smoke：需要 Docker daemon 与显式指定的镜像，否则 skip。

CI 无 daemon 时自动跳过；本地构建完 `docker/sandbox.Dockerfile` 后：

    DEVOPS_AGENT_SANDBOX_IMAGE=devops-agent-sandbox:2.0.10 \
      PYTHONPATH=src .venv/bin/python -m pytest tests/integration/test_sandbox_image.py
"""

from __future__ import annotations

import os
import subprocess

import pytest

from devops_agent.executor.manager import docker_available

_DAEMON_OK, _DAEMON_WHY = docker_available()
pytestmark = pytest.mark.skipif(not _DAEMON_OK, reason=f"docker daemon 不可用（{_DAEMON_WHY}）")

_FAST_PATH_SCRIPT = (
    "test -f /root/.agentscope/_mcp_gateway_app.py && "
    "test -f /root/.agentscope/_glob_helper.py && "
    "test -x /root/.agentscope/.venv/bin/python && "
    "rg --version >/dev/null && pwd"
)


def test_prebuilt_image_has_gateway_fast_path() -> None:
    """镜像里已存在脚本与 venv 时，运行时 bootstrap 会被整体跳过。"""
    image = os.environ.get("DEVOPS_AGENT_SANDBOX_IMAGE")
    if not image:
        pytest.skip("设置 DEVOPS_AGENT_SANDBOX_IMAGE 后运行镜像 smoke")
    result = subprocess.run(
        ["docker", "run", "--rm", image, "sh", "-c", _FAST_PATH_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("/workspace")
