"""会话注册表的工作区归属（SPEC-0021 A6）。

本文件依赖 runtime 包（间接 import agentscope），本地缺框架时由 CI 运行。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from devops_agent.governance.model import PROFILES
from devops_agent.runtime.sessions import SessionRegistry
from devops_agent.workspace.models import WorkspaceRef


class FakeRuntime:
    """最小替身：只需可序列化状态与热替换能力。"""

    def __init__(self, state: str = "") -> None:
        self.state = state

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            yield {"type": "TEXT_BLOCK_DELTA", "delta": user_input}

        return gen()

    def snapshot(self) -> str:
        return json.dumps({"state": self.state})

    def apply_profile(self, profile: Any, *, web_search: bool | None = None) -> None:
        return None


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sessions.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(path))
    return path


def test_workspace_binds_workdir_and_rebuilds_on_change(db: Path) -> None:
    """A6：绑定工作区后 entry 记录 id；换工作区重建；None 表示沿用。"""
    seen: list[str] = []

    def factory(
        session_id: str,
        profile: Any,
        state_json: str | None,
        web_search: bool = False,
        model_config: dict[str, Any] | None = None,
        workspace: WorkspaceRef | None = None,
    ) -> FakeRuntime:
        seen.append(workspace.id if workspace is not None else "")
        return FakeRuntime()

    registry = SessionRegistry(factory)
    alpha = WorkspaceRef(id="ws_a", root_path="/tmp/a")
    beta = WorkspaceRef(id="ws_b", root_path="/tmp/b")

    first = registry.acquire("s1", PROFILES["workspace"], workspace=alpha)
    assert first.workspace_id == "ws_a"
    assert seen == ["ws_a"]

    again = registry.acquire("s1", PROFILES["workspace"], workspace=alpha)
    assert again is first
    assert seen == ["ws_a"]

    changed = registry.acquire("s1", PROFILES["workspace"], workspace=beta)
    assert changed.workspace_id == "ws_b"
    assert seen == ["ws_a", "ws_b"]

    # workspace=None 表示「不改归属」：沿用当前实例，而不是退回旧路径
    kept = registry.acquire("s1", PROFILES["workspace"])
    assert kept is changed
    assert seen == ["ws_a", "ws_b"]
