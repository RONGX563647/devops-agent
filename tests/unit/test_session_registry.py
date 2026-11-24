"""会话隔离测试：实例复用、档位切换、DB 快照重建、LRU 回收。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from devops_agent.governance.profiles import PROFILES
from devops_agent.observability import repository as repo
from devops_agent.runtime.runtime import EchoRuntime
from devops_agent.runtime.sessions import SessionRegistry


class FakeRuntime:
    """最小时钟：带可序列化状态，用来观察工厂是否被正确调用。"""

    def __init__(self, state: str = "") -> None:
        self.state = state
        self.profiles: list[str] = []

    def apply_profile(self, profile: Any, *, web_search: bool | None = None) -> None:
        """热替换能力：记录档位，供切档测试断言不重建。"""
        self.profiles.append(profile.key)

    async def run(self, session_id: str, user_input: str) -> str:
        return user_input

    def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            self.state += user_input
            yield {"type": "TEXT_BLOCK_DELTA", "delta": user_input}

        return gen()

    def snapshot(self) -> str:
        return json.dumps({"state": self.state})


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sessions.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(path))
    return path


def _registry(calls: list[tuple[str, str, str | None]], **kwargs: Any) -> SessionRegistry:
    def factory(
        session_id: str,
        profile: Any,
        state_json: str | None,
        web_search: bool = False,
        model_config: dict[str, Any] | None = None,
    ) -> FakeRuntime:
        calls.append((session_id, profile.key, state_json))
        saved = json.loads(state_json)["state"] if state_json else ""
        return FakeRuntime(saved)

    return SessionRegistry(factory, **kwargs)


def test_same_session_reuses_instance_but_sessions_do_not_share(db: Path) -> None:
    """核心契约：同会话复用、跨会话隔离（历史与权限规则不串味）。"""
    calls: list[tuple[str, str, str | None]] = []
    registry = _registry(calls)

    first = registry.acquire("s1", PROFILES["workspace"])
    again = registry.acquire("s1", PROFILES["workspace"])
    other = registry.acquire("s2", PROFILES["workspace"])

    assert first is again
    assert first.runtime is not other.runtime
    assert len(calls) == 2


def test_switching_profile_rebuilds_and_persists_state(db: Path) -> None:
    calls: list[tuple[str, str, str | None]] = []
    registry = _registry(calls)
    entry = registry.acquire("s1", PROFILES["workspace"])
    entry.runtime.state = "前半段"

    switched = registry.acquire("s1", PROFILES["unrestricted"])

    assert switched is not entry
    assert switched.profile == "unrestricted"
    # 状态先落库，再用新档位重建：切档不该丢历史
    assert calls[-1][1] == "unrestricted"
    assert json.loads(str(calls[-1][2]))["state"] == "前半段"


def test_switching_web_search_rebuilds_but_confirm_reuses(db: Path) -> None:
    """联网开关变了要重建；确认请求不带开关（None）时必须沿用现有实例。"""
    seen: list[bool] = []

    def factory(
        session_id: str,
        profile: Any,
        state_json: str | None,
        web_search: bool = False,
        model_config: dict[str, Any] | None = None,
    ) -> FakeRuntime:
        seen.append(web_search)
        return FakeRuntime("")

    registry = SessionRegistry(factory, db_path=db)
    off = registry.acquire("s1", PROFILES["workspace"], False)
    assert registry.acquire("s1", PROFILES["workspace"], False) is off

    on = registry.acquire("s1", PROFILES["workspace"], True)
    assert on is not off
    assert seen == [False, True]

    # 续跑/确认没带 web_search：沿用挂起中的实例，绝不重建
    assert registry.acquire("s1", PROFILES["workspace"]) is on


def test_omitted_web_search_follows_deployment_default(db: Path) -> None:
    """请求里省略联网字段 = 按部署默认，不能被压成 False。

    以前 `_build` 写的是 `bool(web_search)`，None 变成 False：
    `DEVOPS_AGENT_ENABLE_SEARCH=1` 的部署在一次会话重建之后就静默关掉了检索。
    """
    built: list[bool | None] = []

    class EnvDefaultRuntime(FakeRuntime):
        """模拟真实运行时：自己解析部署默认，外部只看到解析后的布尔值。"""

        def __init__(self, requested: bool | None) -> None:
            super().__init__("")
            self.web_search = True if requested is None else requested

    def factory(
        session_id: str,
        profile: Any,
        state_json: str | None,
        web_search: bool | None = None,
        model_config: dict[str, Any] | None = None,
    ) -> FakeRuntime:
        built.append(web_search)
        return EnvDefaultRuntime(web_search)

    registry = SessionRegistry(factory, db_path=db)
    entry = registry.acquire("s1", PROFILES["workspace"])

    assert built == [None]  # 原样透传给运行时，不在注册表里提前拍板
    assert entry.web_search is True  # 记的是实际生效值
    # 下一次省略字段时命中复用，不会因为「None != True」反复重建
    assert registry.acquire("s1", PROFILES["workspace"]) is entry
    # 显式关闭仍然要重建：显式 > 部署默认
    assert registry.acquire("s1", PROFILES["workspace"], False) is not entry


def test_acquire_restores_snapshot_from_db(db: Path) -> None:
    """缓存未命中（如进程重启 / 另一个实例）时从库里重建 Agent。"""
    with repo.connect(db) as conn:
        repo.init_db(conn)
        repo.save_session_state(
            conn,
            "s1",
            state_json='{"state": "上次的对话"}',
            permission="workspace",
            updated_at="now",
        )
    calls: list[tuple[str, str, str | None]] = []
    _registry(calls).acquire("s1", PROFILES["workspace"])

    assert calls[0][2] == '{"state": "上次的对话"}'


def test_switch_hot_replaces_without_rebuilding(db: Path) -> None:
    """会话中切档：原地改现有实例的权限，工厂不能重建（会打断正在跑的流）。"""
    calls: list[tuple[str, str, str | None]] = []
    registry = _registry(calls)
    entry = registry.acquire("s1", PROFILES["readonly"])

    switched, applied = registry.switch("s1", PROFILES["execute"])

    assert applied is True
    assert switched is entry
    assert registry.peek("s1") is entry
    assert len(calls) == 1
    assert entry.profile == "execute"
    assert entry.runtime.profiles == ["execute"]
    # 快照里的档位也换了：刷新重建仍按新档装配
    with repo.connect(db) as conn:
        saved = repo.load_session_state(conn, "s1")
    assert saved is not None and saved[1] == "execute"


def test_switch_without_live_entry_falls_back_to_build(db: Path) -> None:
    """没有活跃实例时不能 404：之后发起的这一轮要按新档位装配。"""
    calls: list[tuple[str, str, str | None]] = []
    registry = _registry(calls)
    entry, applied = registry.switch("s1", PROFILES["unrestricted"])

    assert applied is False
    assert entry.profile == "unrestricted"
    assert calls[0][1] == "unrestricted"


def test_save_writes_snapshot_with_permission(db: Path) -> None:
    registry = _registry([])
    entry = registry.acquire("s1", PROFILES["execute"])
    entry.runtime.state = "落库"
    registry.save("s1")

    with repo.connect(db) as conn:
        saved = repo.load_session_state(conn, "s1")
    assert saved is not None
    assert json.loads(saved[0])["state"] == "落库"
    assert saved[1] == "execute"


def test_save_skips_runtime_without_snapshot(db: Path) -> None:
    """Echo 之类无状态运行时没有 snapshot，写库应静默跳过。"""
    registry = SessionRegistry(lambda *_args, **_kw: EchoRuntime(), db_path=db)
    registry.acquire("s1", PROFILES["workspace"])
    registry.save("s1")
    with repo.connect(db) as conn:
        assert repo.load_session_state(conn, "s1") is None


def test_lru_evicts_idle_session(db: Path) -> None:
    registry = _registry([], max_entries=2)
    registry.acquire("s1", PROFILES["workspace"])
    registry.acquire("s2", PROFILES["workspace"])
    registry.acquire("s3", PROFILES["workspace"])
    assert "s1" not in registry.sessions
    assert len(registry.sessions) == 2


def test_lru_never_evicts_running_session(db: Path) -> None:
    """挂起 / 运行中的会话不能被回收：否则 confirm 找不到它。"""
    registry = _registry([], max_entries=1)
    busy = registry.acquire("s1", PROFILES["workspace"])

    async def hold() -> None:
        async with busy.lock:
            registry.acquire("s2", PROFILES["workspace"])
            registry.acquire("s3", PROFILES["workspace"])
            assert "s1" in registry.sessions

    asyncio.run(hold())
