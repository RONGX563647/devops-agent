"""模型参数端点测试：三层覆盖、热替换、deferred、乐观并发、错误码。

用假运行时（能收 apply_model_params）驱动，不发真实模型请求：
这里要验的是「参数有没有真的走到运行时」，不是模型回什么。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devops_agent import config as cfg
from devops_agent import model_settings as ms
from devops_agent.governance.profiles import PROFILES
from devops_agent.main import app
from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.agent import session_runtime_factory
from devops_agent.runtime.sessions import SessionRegistry

client = TestClient(app)


class HotRuntime:
    """能热替换参数的假运行时：只记「收到过哪组参数」。"""

    def __init__(self) -> None:
        self.applied: list[Any] = []

    def apply_model_params(self, params: Any) -> None:
        self.applied.append(params)

    def snapshot(self) -> str:
        return "{}"


@pytest.fixture
def hot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HotRuntime]:
    original = app.state.runtime
    runtime = HotRuntime()
    app.state.runtime = runtime
    monkeypatch.setenv(repo.DB_PATH_ENV, str(tmp_path / "model.db"))
    monkeypatch.delenv(ms.MODEL_ENV, raising=False)
    cfg.clear_cache()
    ms.invalidate()
    app.state.sessions = None
    app.state.sessions_base = None
    try:
        yield runtime
    finally:
        app.state.runtime = original
        app.state.sessions = None
        app.state.sessions_base = None
        cfg.clear_cache()
        ms.invalidate()


def _entry(session: str = "s1"):
    """建一份和端点共用的注册表，并放一个活跃会话进去。

    注册表平时由 `registry_for` 懒建；这里显式建好挂到 app.state，
    端点取到的就是同一个实例，测试才能直接操作那个 entry。
    """
    registry = SessionRegistry(session_runtime_factory(app.state.runtime))
    app.state.sessions = registry
    app.state.sessions_base = app.state.runtime
    return registry, registry.acquire(session, PROFILES["workspace"])


def test_defaults_expose_sources_and_derived(hot: HotRuntime) -> None:
    body = client.get("/api/model-config").json()
    assert body["effective"]["context_size"] == 128_000
    assert body["sources"]["context_size"] == "yaml"
    assert body["derived"]["compression_threshold"] == 102_400
    # 两级重试必须能算出乘积，而不是被合成一个数
    assert body["derived"]["worst_case_attempts"] == 16
    assert body["derived"]["context_uncalibrated"] is True
    assert "temperature" in body["editable"]
    assert "api_key" in body["readonly"]
    assert body["bounds"]["temperature"]["max"] == 2
    assert body["version"] == 0


def test_patch_deployment_default_applies_to_active_sessions(hot: HotRuntime) -> None:
    _entry()
    body = client.patch("/api/model-config", json={"patch": {"temperature": 0.3}}).json()
    assert body["effective"]["temperature"] == 0.3
    assert body["sources"]["temperature"] == "stored"
    assert body["version"] == 1
    assert body["affected"] == {"applied": 1, "deferred": 0}
    assert hot.applied[-1].temperature == 0.3


def test_patch_rejects_stale_version(hot: HotRuntime) -> None:
    client.patch("/api/model-config", json={"patch": {"temperature": 0.3}})
    resp = client.patch("/api/model-config", json={"patch": {"temperature": 0.4}, "version": 0})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "config_version_conflict"
    # 冲突要带回当前版本与现值，前端才能让用户选「覆盖」还是「放弃」
    assert detail["version"] == 1
    assert detail["effective"]["temperature"] == 0.3


def test_patch_rejects_invalid_cross_field(hot: HotRuntime) -> None:
    _entry()
    resp = client.patch(
        "/api/model-config", json={"patch": {"trigger_ratio": 0.5, "reserve_ratio": 0.8}}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_model_config"
    assert detail["field"] == "reserve_ratio"
    assert detail["bounds"]["max"] == 0.89


def test_patch_rejects_out_of_range(hot: HotRuntime) -> None:
    resp = client.patch("/api/model-config", json={"patch": {"temperature": 9}})
    assert resp.status_code == 400
    assert resp.json()["detail"]["field"] == "temperature"


def test_patch_rejects_readonly_key(hot: HotRuntime) -> None:
    resp = client.patch("/api/model-config", json={"patch": {"api_key": "sk-x"}})
    assert resp.status_code == 400
    assert resp.json()["detail"]["field"] == "api_key"


def test_session_override_wins_and_is_hot_swapped(hot: HotRuntime) -> None:
    _entry()
    body = client.patch(
        "/api/session/s1/model-config", json={"overrides": {"temperature": 0.2, "max_iters": 30}}
    ).json()
    assert body["sources"]["temperature"] == "session"
    assert body["sources"]["max_iters"] == "session"
    assert body["applied"] is True
    assert body["effective_from"] == "下一次模型调用"
    assert hot.applied[-1].temperature == 0.2
    assert hot.applied[-1].max_iters == 30
    # 合并语义：另一次 PATCH 只改一项，不把之前的覆盖冲掉
    body = client.patch(
        "/api/session/s1/model-config", json={"overrides": {"top_p": 0.5}}
    ).json()
    assert body["overrides"] == {"temperature": 0.2, "max_iters": 30, "top_p": 0.5}


def test_session_mismatch_is_rejected(hot: HotRuntime) -> None:
    _entry()
    resp = client.patch(
        "/api/session/s1/model-config",
        json={"overrides": {"temperature": 0.2}, "session_id": "other"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "session_mismatch"


def test_session_delete_falls_back_to_defaults(hot: HotRuntime) -> None:
    _entry()
    client.patch("/api/session/s1/model-config", json={"overrides": {"temperature": 0.2}})
    body = client.delete("/api/session/s1/model-config").json()
    assert body["cleared"] is True
    assert body["sources"]["temperature"] == "yaml"
    assert body["overrides"] == {}
    assert hot.applied[-1].temperature is None


def test_override_survives_registry_rebuild(hot: HotRuntime) -> None:
    """覆盖必须落库：LRU 淘汰后重建也要带上，不能只在内存里。"""
    _entry()
    client.patch("/api/session/s1/model-config", json={"overrides": {"temperature": 0.2}})
    registry = app.state.sessions
    registry.drop("s1")
    entry = registry.acquire("s1", PROFILES["workspace"])
    assert entry.model_overrides == {"temperature": 0.2}


def test_running_session_is_deferred_then_flushed(hot: HotRuntime) -> None:
    """正在跑的会话不打断：先记 pending，run 结束后补上。"""
    registry, entry = _entry()
    entry.lock._locked = True  # 模拟「这一轮正在跑」
    body = client.patch("/api/model-config", json={"patch": {"temperature": 0.4}}).json()
    assert body["affected"] == {"applied": 0, "deferred": 1}
    assert entry.pending_model is True
    # 跑着的会话不能被改
    assert all(item.temperature != 0.4 for item in hot.applied)

    entry.lock._locked = False
    assert registry.flush_pending("s1") is True
    assert hot.applied[-1].temperature == 0.4


def test_reset_clears_deployment_defaults(hot: HotRuntime) -> None:
    client.patch("/api/model-config", json={"patch": {"temperature": 0.3, "max_iters": 30}})
    body = client.post("/api/model-config/reset", json={"keys": ["temperature"]}).json()
    assert body["effective"]["temperature"] is None
    assert body["effective"]["max_iters"] == 30
    assert body["sources"]["temperature"] == "yaml"
    body = client.post("/api/model-config/reset", json={"all": True}).json()
    assert body["effective"]["max_iters"] == 12
    assert body["version"] >= 3


def test_run_record_keeps_only_non_default_params(hot: HotRuntime) -> None:
    """run 记录里的参数快照：只含非默认值，改一项才变。"""
    db = repo.default_db_path()
    params = ms.ModelParams.from_values({"temperature": 0.3, "model": "qwen-test"})
    recorder = RunRecorder(db_path=db, model="qwen-test", model_params=params.record())
    run_id = recorder.start("s1", "hi")
    recorder.finish()
    with repo.connect(db) as conn:
        run = repo.get_run(conn, run_id)
    assert run is not None
    assert run.model_params == '{"model": "qwen-test", "temperature": 0.3}'
