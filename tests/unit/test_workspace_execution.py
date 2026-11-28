"""工作区 → 执行目录的接线（这条链路断过一次，必须钉住）。

现场：会话确实绑在 `E:\\code\\devops-agent\\workspace\\workspaces\\ws_xxx\\repo`
（`runs.workspace_id` 有值），模型却说「我在 E:\\code\\devops-agent，我没有 clone
过任何仓库」。根因在装配层：一旦 `execution` 非空，`_execution_paths` 就以它的
`host_workdir` 为准，把 `workdir` 整个丢掉；而会话工厂恰好传的是**启动期**那一份，
于是每个工作区都被钉在 `DEVOPS_AGENT_WORKDIR` 上跑。

这里钉四件事：执行面按工作区取、取不到就 409、工厂优先用本轮执行面、
执行面换了要重建会话实例。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from devops_agent.gateway.routes.workspace_deps import execution_for
from devops_agent.runtime import assembly
from devops_agent.runtime.scope_runtime import AgentScopeRuntime
from devops_agent.runtime.sessions import SessionRegistry
from devops_agent.runtime.workspace_note import workspace_brief
from devops_agent.workspace.models import WorkspaceRef

_PROFILE = SimpleNamespace(key="workspace")


def _execution(host: str) -> SimpleNamespace:
    """够用的执行面替身：运行时只读这几个字段。"""
    return SimpleNamespace(
        host_workdir=Path(host),
        tool_workdir=host,
        shell=None,
        uses_shared_backend=False,
        backend_factory=None,
        mode="local",
    )


def _request(manager: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sandbox_manager=manager)))


class _Manager:
    """只记住被要求准备哪个目录；真实初始化由 SandboxManager 自己做。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure(self, host_workdir: Any, **_kwargs: Any) -> Any:
        self.calls.append(str(host_workdir))
        return SimpleNamespace(host_workdir=Path(host_workdir), tool_workdir=str(host_workdir))


def test_execution_follows_workspace_root(tmp_path: Path) -> None:
    """工作区根就是执行面目录：不是启动期目录，也不是别的什么。"""
    manager = _Manager()
    ref = WorkspaceRef(id="ws_1", root_path=str(tmp_path))
    target = asyncio.run(execution_for(_request(manager), ref))
    assert manager.calls == [str(tmp_path)]
    assert target is not None


def test_unbound_session_keeps_startup_execution(tmp_path: Path) -> None:
    """未绑定工作区：返回 None，调用方沿用启动期执行面（旧行为不变）。"""
    manager = _Manager()
    assert asyncio.run(execution_for(_request(manager), None)) is None
    assert manager.calls == []


def test_missing_workspace_dir_is_409_not_an_empty_workspace(tmp_path: Path) -> None:
    """目录没了要 409：执行面会按需建目录，放任下去就是一个空工作区。"""
    manager = _Manager()
    gone = WorkspaceRef(id="ws_2", root_path=str(tmp_path / "deleted"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(execution_for(_request(manager), gone))
    assert exc.value.status_code == 409
    assert exc.value.detail == "workspace_root_missing"
    assert manager.calls == []


def test_factory_prefers_this_turn_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """本轮执行面优先于启动期：否则工作区绑了也等于没绑。"""
    captured: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return "runtime"

    monkeypatch.setattr(assembly, "build_agentscope_runtime", fake_build)
    startup = _execution("/startup")
    base = AgentScopeRuntime(object(), execution=startup)  # type: ignore[arg-type]
    make = assembly.session_runtime_factory(base)
    ref = WorkspaceRef(
        id="ws_1",
        root_path="C:/code/repo",
        name="demo",
        source="worktree",
        branch="agent/demo-1",
    )

    make("s1", _PROFILE, None, workspace=ref, execution="PER_WORKSPACE")

    assert captured["execution"] == "PER_WORKSPACE"
    assert captured["workdir"] == "C:/code/repo"
    assert captured["workspace_id"] == "ws_1"
    assert "demo" in captured["workspace_brief"]


def test_factory_falls_back_to_startup_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """没给本轮执行面时仍用启动期那一份：未绑定会话的行为不变。"""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        assembly, "build_agentscope_runtime", lambda **kw: captured.update(kw) or "runtime"
    )
    startup = _execution("/startup")
    base = AgentScopeRuntime(object(), execution=startup)  # type: ignore[arg-type]
    assembly.session_runtime_factory(base)("s1", _PROFILE, None)
    assert captured["execution"] is startup
    assert captured["workdir"] is None


def test_registry_rebuilds_when_execution_workdir_changes(tmp_path: Path) -> None:
    """同一工作区换了一份执行面也要重建：否则工具还跑在旧目录。"""
    built: list[dict[str, Any]] = []

    def factory(session_id: str, profile: Any, state: Any, web_search: Any, **kwargs: Any) -> Any:
        built.append(kwargs)
        return SimpleNamespace()

    registry = SessionRegistry(factory, db_path=tmp_path / "obs.db", max_entries=4)
    ref = WorkspaceRef(id="ws_1", root_path=str(tmp_path))
    first = registry.acquire(
        "s1", _PROFILE, None, workspace=ref, execution=SimpleNamespace(host_workdir=tmp_path / "a")
    )
    second = registry.acquire(
        "s1", _PROFILE, None, workspace=ref, execution=SimpleNamespace(host_workdir=tmp_path / "b")
    )
    same = registry.acquire(
        "s1", _PROFILE, None, workspace=ref, execution=SimpleNamespace(host_workdir=tmp_path / "b")
    )

    assert first is not second
    assert same is second
    assert len(built) == 2
    assert second.execution_host == str(tmp_path / "b")


def test_workspace_brief_tells_the_model_where_it_is() -> None:
    """模型要能不看工具就答出「代码在哪」，所以路径、来源、分支、远端都得在。"""
    ref = WorkspaceRef(
        id="ws_1",
        root_path="C:/managed/ws_1/repo",
        name="devops-agent",
        source="git",
        remote_url="https://github.com/org/devops-agent.git",
        branch="main",
    )
    text = workspace_brief(ref)
    assert "C:/managed/ws_1/repo" in text
    assert "devops-agent" in text
    assert "远端克隆" in text
    assert "main" in text
    assert "https://github.com/org/devops-agent.git" in text


def test_workspace_brief_says_so_when_unbound() -> None:
    """没有工作区也要如实说：不能让模型自己编一个「我的工作区」。"""
    text = workspace_brief(None, "C:/startup")
    assert "没有绑定工作区" in text
    assert "C:/startup" in text


def test_real_runtime_actually_runs_in_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真装配一次：运行时的工作目录必须是工作区根，说明必须进模型调用链。

    这是整条链唯一的「观测点」——前面几个用例都在测参数怎么流，只有这里能证明
    最后真的落在 `AgentScopeRuntime` 上。当初的缺陷正是「参数看着对、装配时被换掉」。
    """
    from devops_agent.governance.presets import resolve
    from devops_agent.runtime.workspace_note import WorkspaceNoteMiddleware

    # 只要模型参数齐全，装配阶段不会真的发请求
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "http://localhost:1")
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "dummy")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "stub")

    root = tmp_path / "repo"
    root.mkdir()
    base = AgentScopeRuntime(object(), execution=_execution(str(tmp_path / "startup")))  # type: ignore[arg-type]
    runtime = assembly.session_runtime_factory(base)(
        "s1",
        resolve("workspace"),
        None,
        workspace=WorkspaceRef(
            id="ws_1",
            root_path=str(root),
            name="demo",
            source="worktree",
            branch="agent/demo-1",
        ),
        execution=_execution(str(root)),
    )

    assert Path(runtime._workdir) == root  # noqa: SLF001 - 装配结果只能从这里观测
    assert Path(runtime._tool_workdir) == root  # noqa: SLF001
    chain = runtime._agent._model_call_middlewares  # noqa: SLF001
    notes = [m._brief for m in chain if isinstance(m, WorkspaceNoteMiddleware)]
    assert notes and str(root) in notes[0]
