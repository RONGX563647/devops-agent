"""SandboxManager：缓存、并发、降级、关闭与 TTL 回收（全部用假 workspace）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.tool import LocalBackend
from agentscope.workspace import DockerWorkspace, LocalWorkspace

from devops_agent.config_schema import SandboxPolicy
from devops_agent.executor import manager as manager_mod
from devops_agent.executor.manager import (
    SandboxManager,
    SandboxUnavailable,
    docker_available,
)


class _FakeDocker(DockerWorkspace):
    """不触 Docker 的 DockerWorkspace 替身：只计初始化/关闭次数。"""

    def __init__(self, fail: str = "") -> None:
        super().__init__(base_image="fake:1")
        self._backend = LocalBackend()
        self.fail = fail
        self.init_count = 0
        self.close_count = 0

    async def initialize(self) -> None:
        self.init_count += 1
        if self.fail:
            raise RuntimeError(self.fail)

    async def close(self) -> None:
        self.close_count += 1


class _FakeLocal(LocalWorkspace):
    """不落盘的本机替身：只计初始化/关闭次数。"""

    def __init__(self, workdir: str) -> None:
        super().__init__(workdir=workdir)
        self.init_count = 0
        self.close_count = 0

    async def initialize(self) -> None:
        self.init_count += 1

    async def close(self) -> None:
        self.close_count += 1


class _Factory:
    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.made: list[object] = []

    def __call__(self, mode: str, host: Path, policy: SandboxPolicy):
        self.calls.append(mode)
        if mode == "docker":
            workspace = _FakeDocker(self.fail)
        else:
            workspace = _FakeLocal(str(host))
        self.made.append(workspace)
        return workspace


def _probe(ok: bool, reason: str = "docker_unavailable: test") -> object:
    return lambda: (ok, "" if ok else reason)


def _run(coro):
    return asyncio.run(coro)


def test_local_target_is_cached(tmp_path: Path) -> None:
    factory = _Factory()
    sandbox = SandboxManager(SandboxPolicy(mode="local"), workspace_factory=factory)

    async def scenario():
        return await sandbox.ensure(tmp_path), await sandbox.ensure(tmp_path)

    first, second = _run(scenario())
    assert first is second
    assert first.mode == "local"
    assert factory.calls == ["local"]


def test_docker_target_when_daemon_available(tmp_path: Path) -> None:
    factory = _Factory()
    sandbox = SandboxManager(
        SandboxPolicy(mode="docker", required=True),
        workspace_factory=factory,
        probe=_probe(True),
    )
    target = _run(sandbox.ensure(tmp_path))
    assert target.mode == "docker"
    assert target.tool_workdir == "/workspace"


def test_auto_falls_back_with_reason(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="auto"),
        workspace_factory=_Factory(),
        probe=_probe(False, "docker_unavailable: no daemon"),
    )
    target = _run(sandbox.ensure(tmp_path))
    assert target.mode == "local"
    assert "docker_unavailable" in target.degraded_reason


def test_docker_without_required_falls_back(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="docker", required=False),
        workspace_factory=_Factory(),
        probe=_probe(False),
    )
    target = _run(sandbox.ensure(tmp_path))
    assert target.mode == "local"
    assert target.degraded_reason


def test_docker_required_probe_failure_raises(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="docker", required=True),
        workspace_factory=_Factory(),
        probe=_probe(False),
    )
    with pytest.raises(SandboxUnavailable) as info:
        _run(sandbox.ensure(tmp_path))
    assert info.value.code == "sandbox_unavailable"


def test_image_build_failure_classified(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="docker", required=True),
        workspace_factory=_Factory(fail="image build failed"),
        probe=_probe(True),
    )
    with pytest.raises(SandboxUnavailable) as info:
        _run(sandbox.ensure(tmp_path))
    assert info.value.code == "sandbox_image_build_failed"


def test_start_failure_falls_back_when_not_required(tmp_path: Path) -> None:
    factory = _Factory(fail="container start failed")
    sandbox = SandboxManager(
        SandboxPolicy(mode="docker", required=False),
        workspace_factory=factory,
        probe=_probe(True),
    )
    target = _run(sandbox.ensure(tmp_path))
    assert target.mode == "local"
    assert "sandbox_start_failed" in target.degraded_reason
    assert factory.calls == ["docker", "local"]


def test_failure_is_not_cached(tmp_path: Path) -> None:
    factory = _Factory(fail="boom")
    sandbox = SandboxManager(
        SandboxPolicy(mode="docker", required=True),
        workspace_factory=factory,
        probe=_probe(True),
    )

    async def scenario() -> int:
        failures = 0
        for _ in range(2):
            try:
                await sandbox.ensure(tmp_path)
            except SandboxUnavailable:
                failures += 1
        return failures

    assert _run(scenario()) == 2
    assert factory.calls == ["docker", "docker"]


def test_concurrent_ensure_initializes_once(tmp_path: Path) -> None:
    factory = _Factory()
    sandbox = SandboxManager(
        SandboxPolicy(mode="docker", required=True),
        workspace_factory=factory,
        probe=_probe(True),
    )

    async def scenario():
        return await asyncio.gather(*(sandbox.ensure(tmp_path) for _ in range(5)))

    targets = _run(scenario())
    assert all(item is targets[0] for item in targets)
    assert factory.calls == ["docker"]


def test_close_all_is_idempotent(tmp_path: Path) -> None:
    factory = _Factory()
    sandbox = SandboxManager(SandboxPolicy(mode="local"), workspace_factory=factory)

    async def scenario():
        await sandbox.ensure(tmp_path)
        await sandbox.close_all()
        await sandbox.close_all()

    _run(scenario())
    assert factory.made and factory.made[0].close_count == 1  # type: ignore[attr-defined]


def test_workspace_context_manager_closes(tmp_path: Path) -> None:
    factory = _Factory()
    sandbox = SandboxManager(SandboxPolicy(mode="local"), workspace_factory=factory)

    async def scenario():
        async with sandbox.workspace(tmp_path) as target:
            assert target.mode == "local"

    _run(scenario())
    assert factory.made[0].close_count == 1  # type: ignore[attr-defined]


def test_reap_idle_only_closes_expired_reapable(tmp_path: Path) -> None:
    now = [1000.0]
    factory = _Factory()
    sandbox = SandboxManager(
        SandboxPolicy(mode="local", ttl_seconds=10),
        workspace_factory=factory,
        clock=lambda: now[0],
    )

    async def scenario() -> int:
        await sandbox.ensure(tmp_path / "chat", reapable=False)
        await sandbox.ensure(tmp_path / "case", reapable=True)
        now[0] += 11
        return await sandbox.reap_idle()

    assert _run(scenario()) == 1
    closed = [item.close_count for item in factory.made]  # type: ignore[attr-defined]
    assert closed == [0, 1]


def test_status_reports_effective_mode(tmp_path: Path) -> None:
    sandbox = SandboxManager(SandboxPolicy(mode="local"), workspace_factory=_Factory())
    _run(sandbox.ensure(tmp_path))
    status = sandbox.status()
    assert status["mode"] == "local"
    assert status["effective"] == "local"
    assert status["containers"] == 1
    assert status["host_workdir"] == str(tmp_path)


def test_docker_available_without_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod.shutil, "which", lambda _name: None)
    ok, reason = docker_available()
    assert ok is False
    assert reason.startswith("docker_unavailable")


def test_docker_available_reports_daemon_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        manager_mod.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="Cannot connect", stdout=""),
    )
    ok, reason = docker_available()
    assert ok is False
    assert "Cannot connect" in reason


def test_docker_available_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        manager_mod.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )
    assert docker_available() == (True, "")
