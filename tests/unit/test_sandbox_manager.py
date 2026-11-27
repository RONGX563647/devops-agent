"""SandboxManager：缓存、并发、降级、关闭与 TTL 回收（全部用假 workspace）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.tool import LocalBackend
from agentscope.workspace import DockerWorkspace, LocalWorkspace, OpenSandboxWorkspace

from devops_agent.config_schema import SandboxPolicy
from devops_agent.executor import manager as manager_mod
from devops_agent.executor.boundary import (
    build_opensandbox_workspace,
    opensandbox_workspace_id,
)
from devops_agent.executor.manager import (
    SandboxManager,
    SandboxUnavailable,
    docker_available,
    opensandbox_available,
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


class _FakeOpenSandbox(OpenSandboxWorkspace):
    """不触远端服务的 OpenSandboxWorkspace 替身。"""

    def __init__(self, fail: str = "") -> None:
        super().__init__(image="fake:1")
        self._backend = LocalBackend()
        self._sandbox = SimpleNamespace(id="sbx-test")
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
        elif mode == "opensandbox":
            workspace = _FakeOpenSandbox(self.fail)
        else:
            workspace = _FakeLocal(str(host))
        self.made.append(workspace)
        return workspace


def _probe(ok: bool, reason: str = "docker_unavailable: test") -> object:
    return lambda: (ok, "" if ok else reason)


def _osb_probe(ok: bool, reason: str = "sandbox_package_missing: test") -> object:
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


# --- OpenSandbox（SPEC-0019）--------------------------------------------


def test_opensandbox_available_reports_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod.importlib.util, "find_spec", lambda _name: None)
    ok, reason = opensandbox_available(SandboxPolicy(mode="opensandbox"))
    assert ok is False
    assert "sandbox_package_missing" in reason


def test_opensandbox_available_requires_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod.importlib.util, "find_spec", lambda _name: object())
    ok, reason = opensandbox_available(SandboxPolicy(mode="opensandbox"))
    assert ok is False
    assert "sandbox_endpoint_missing" in reason


def test_opensandbox_available_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_mod.importlib.util, "find_spec", lambda _name: object())
    policy = SandboxPolicy(mode="opensandbox", opensandbox={"domain": "osb.example.com"})
    assert opensandbox_available(policy) == (True, "")


def test_opensandbox_target_when_probe_ok(tmp_path: Path) -> None:
    factory = _Factory()
    sandbox = SandboxManager(
        SandboxPolicy(mode="opensandbox", required=True),
        workspace_factory=factory,
        opensandbox_probe=_osb_probe(True),
    )
    target = _run(sandbox.ensure(tmp_path))
    assert target.mode == "opensandbox"
    assert target.tool_workdir == "/workspace"
    assert target.sandbox_id == "sbx-test"
    assert target.uses_shared_backend is True
    assert factory.calls == ["opensandbox"]


def test_opensandbox_required_probe_failure_uses_reason_code(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="opensandbox", required=True),
        workspace_factory=_Factory(),
        opensandbox_probe=_osb_probe(False, "sandbox_package_missing: 未安装"),
    )
    with pytest.raises(SandboxUnavailable) as info:
        _run(sandbox.ensure(tmp_path))
    assert info.value.code == "sandbox_package_missing"


def test_auto_prefers_opensandbox(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="auto"),
        workspace_factory=_Factory(),
        probe=_probe(True),
        opensandbox_probe=_osb_probe(True),
    )
    assert _run(sandbox.ensure(tmp_path)).mode == "opensandbox"


def test_auto_falls_back_to_docker_when_opensandbox_missing(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="auto"),
        workspace_factory=_Factory(),
        probe=_probe(True),
        opensandbox_probe=_osb_probe(False),
    )
    assert _run(sandbox.ensure(tmp_path)).mode == "docker"


def test_auto_records_both_remote_failures(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="auto"),
        workspace_factory=_Factory(),
        probe=_probe(False, "docker_unavailable: no daemon"),
        opensandbox_probe=_osb_probe(False, "sandbox_package_missing: nope"),
    )
    target = _run(sandbox.ensure(tmp_path))
    assert target.mode == "local"
    assert "sandbox_package_missing" in target.degraded_reason
    assert "docker_unavailable" in target.degraded_reason


def test_opensandbox_auth_failure_classified(tmp_path: Path) -> None:
    sandbox = SandboxManager(
        SandboxPolicy(mode="opensandbox", required=True),
        workspace_factory=_Factory(fail="401 unauthorized"),
        opensandbox_probe=_osb_probe(True),
    )
    with pytest.raises(SandboxUnavailable) as info:
        _run(sandbox.ensure(tmp_path))
    assert info.value.code == "sandbox_auth_failed"


def test_opensandbox_bootstrap_failure_falls_back(tmp_path: Path) -> None:
    factory = _Factory(fail="bootstrap failed")
    sandbox = SandboxManager(
        SandboxPolicy(mode="opensandbox", required=False),
        workspace_factory=factory,
        opensandbox_probe=_osb_probe(True),
    )
    target = _run(sandbox.ensure(tmp_path))
    assert target.mode == "local"
    assert "sandbox_bootstrap_failed" in target.degraded_reason
    assert factory.calls == ["opensandbox", "local"]


def test_opensandbox_image_participates_in_cache_key(tmp_path: Path) -> None:
    policy = SandboxPolicy(mode="opensandbox", opensandbox={"image": "img:a"})
    sandbox = SandboxManager(policy, workspace_factory=_Factory())
    assert sandbox._key("opensandbox", tmp_path)[2] == "img:a"
    assert sandbox._key("local", tmp_path)[2] == ""


def test_bootstrap_skipped_uses_init_threshold() -> None:
    assert SandboxManager._bootstrap_skipped("opensandbox", 1200) is True
    assert SandboxManager._bootstrap_skipped("opensandbox", 35_000) is False
    assert SandboxManager._bootstrap_skipped("opensandbox", 0) is None
    assert SandboxManager._bootstrap_skipped("docker", 1200) is None


def test_status_reports_opensandbox_fields(tmp_path: Path) -> None:
    policy = SandboxPolicy(
        mode="opensandbox",
        opensandbox={"image": "img:golden", "domain": "osb.example.com"},
    )
    sandbox = SandboxManager(
        policy, workspace_factory=_Factory(), opensandbox_probe=_osb_probe(True)
    )
    _run(sandbox.ensure(tmp_path))
    status = sandbox.status()
    assert status["provider"] == "opensandbox"
    assert status["effective"] == "opensandbox"
    assert status["sandbox_id"] == "sbx-test"
    assert status["image"] == "img:golden"
    assert status["reused"] is None
    assert "bootstrap_skipped" in status


def test_default_factory_builds_opensandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_OSB_API_KEY", "secret")
    policy = SandboxPolicy(
        mode="opensandbox",
        opensandbox={
            "image": "img:1",
            "domain": "osb.example.com",
            "protocol": "http",
            "entrypoint": ["sh"],
            "env": {"K": "V"},
            "resource": {"cpu": "1"},
        },
    )
    workspace = manager_mod._default_factory("opensandbox", tmp_path, policy)

    assert isinstance(workspace, OpenSandboxWorkspace)
    assert workspace.workspace_id == opensandbox_workspace_id(str(tmp_path))
    assert workspace.image == "img:1"
    assert workspace.api_key == "secret"
    assert workspace.domain == "osb.example.com"
    assert workspace.protocol == "http"
    assert workspace.entrypoint == ["sh"]
    assert workspace.env == {"K": "V"}
    assert workspace.resource == {"cpu": "1"}
    assert workspace.network_policy is None


def test_network_policy_dict_is_converted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置是 dict，SDK 要 NetworkPolicy；转换在构造期完成。"""
    import sys
    import types

    fake_sandboxes = types.ModuleType("opensandbox.models.sandboxes")
    captured: dict[str, object] = {}

    class _NetworkPolicy:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_sandboxes.NetworkPolicy = _NetworkPolicy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opensandbox", types.ModuleType("opensandbox"))
    monkeypatch.setitem(sys.modules, "opensandbox.models", types.ModuleType("opensandbox.models"))
    monkeypatch.setitem(sys.modules, "opensandbox.models.sandboxes", fake_sandboxes)

    build_opensandbox_workspace(
        workspace_id="ws-1",
        network_policy={"egress": "deny"},
    )
    assert captured == {"egress": "deny"}
