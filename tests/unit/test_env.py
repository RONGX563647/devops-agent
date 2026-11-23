"""本地 .env 加载单元测试。"""

from __future__ import annotations

import os
from pathlib import Path

from devops_agent.env import load_env

FLAG = "DEVOPS_AGENT_TEST_FLAG"


def test_load_env_reads_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{FLAG}=from_file\n", encoding="utf-8")
    os.environ.pop(FLAG, None)
    assert load_env(env_file) is True
    assert os.environ[FLAG] == "from_file"
    os.environ.pop(FLAG, None)


def test_load_env_does_not_override_existing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{FLAG}=from_file\n", encoding="utf-8")
    os.environ[FLAG] = "from_shell"
    assert load_env(env_file) is True
    assert os.environ[FLAG] == "from_shell"
    os.environ.pop(FLAG, None)


def test_load_env_missing_file_is_noop(tmp_path: Path) -> None:
    assert load_env(tmp_path / "nonexistent.env") is False
