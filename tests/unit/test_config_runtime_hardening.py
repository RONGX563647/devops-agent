"""配置与启动层的硬化：坏库不拦启动、写事务版本单调、失败可见、边界清理。

每条用例对应一次实测过的故障（审计里给了证据），不是「补个覆盖率」：

1. 把 config.db 写成垃圾字节 → 旧实现 import 期就抛，进程起不来；读接口 500。
2. 10 线程并发 patch 同一域 → 旧实现全部返回 v1，乐观锁形同虚设。
3. 坏库时三条读路径三种答复（500 / 静默 / 报错）→ 统一成「读降级 + 留痕、写拒绝」。
4. drift 提示永不消除 → upsert 不更新 default_at_write。
5. `reset` 拼错键静默成功 → 与 `patch` 的 `_check_keys` 对齐。
6. `/health` 的模型名只读环境变量 → 与实际生效的 model_settings 解析不一致。
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent import config as cfg
from devops_agent import config_store as cs
from devops_agent.config_store import health, schema, store
from devops_agent.gateway.app import create_app
from devops_agent.runtime.runtime import EchoRuntime

_GARBAGE = b"not a sqlite database"


@pytest.fixture
def conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """独立的 config/ 与 config.db：不污染仓库，也不吃本机覆盖。"""
    target = tmp_path / "config"
    target.mkdir()
    shutil.copy(cfg.config_dir() / "policy.yaml", target / "policy.yaml")
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(target))
    monkeypatch.setenv(cs.MODE_ENV, "db")
    monkeypatch.setenv(cs.CONFIG_DB_ENV, str(tmp_path / "config.db"))
    cfg.clear_cache()
    cs.invalidate()
    health.reset()
    try:
        yield target
    finally:
        cfg.clear_cache()
        cs.invalidate()
        health.reset()


def _corrupt_db() -> None:
    Path(cfg.config_dir()).parent.joinpath("config.db").write_bytes(_GARBAGE)


# --- P0-1：坏库不该拦住进程与接口 ---------------------------------------


def test_corrupt_db_degrades_to_defaults(conf: Path) -> None:
    """读库失败 → 按「没有覆盖」继续，但 degraded 必须亮起来。"""
    _corrupt_db()
    snapshot = cs.resolved("input")
    assert snapshot.values["max_references"] == cfg.InputPolicy().max_references
    assert snapshot.version == 0

    status = health.snapshot()
    assert status["degraded"] is True
    assert "DatabaseError" in status["error"]
    assert status["layers"]["db"]["failures"] >= 1


def test_corrupt_db_does_not_block_app_startup(conf: Path) -> None:
    """装配根不再因坏库抛出：进程能起来，`/health` 自己说出降级。"""
    _corrupt_db()
    app = create_app(runtime=EchoRuntime())
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/api/config/input").status_code == 200
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["config"]["degraded"] is True
    assert payload["config"]["mode"] == "db"


def test_corrupt_db_rejects_writes_and_audit(conf: Path) -> None:
    """写路径与「覆盖明细」类读取必须拒绝：写进坏库 / 报空列表都是谎报。"""
    _corrupt_db()
    with pytest.raises(cs.ConfigUnavailable):
        cs.patch("input", {"max_references": 9})
    with pytest.raises(cs.ConfigUnavailable):
        cs.audit("input")
    with pytest.raises(cs.ConfigUnavailable):
        cs.export_yaml("input")


def test_corrupt_db_api_returns_503(conf: Path) -> None:
    from fastapi import FastAPI

    from devops_agent.config_store.api import router

    _corrupt_db()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/config/input/audit")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "config_unavailable"


def test_model_db_failure_is_recorded(conf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模型参数的读库失败不再静默：与配置库共用一份健康计数。"""
    import importlib

    import devops_agent.model_settings as model_settings

    # 注意：`model_settings.resolve` 这个名字被门面 re-export 成了函数，
    # `import ... as` 拿到的是函数，必须走 import_module 取模块本体。
    resolve_mod = importlib.import_module("devops_agent.model_settings.resolve")

    def boom() -> None:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(resolve_mod.repo, "connect", boom)
    model_settings.invalidate()
    resolved = model_settings.resolve(stored=model_settings.stored_defaults(force=True))
    assert resolved["model"].value is not None  # 仍回落 yaml / env / 框架默认
    assert health.snapshot()["layers"]["model"]["failures"] >= 1


# --- P0-2：写事务让版本号严格单调 ---------------------------------------


def test_concurrent_patches_get_monotonic_versions(conf: Path) -> None:
    """10 个线程同时写同一域的不同键：版本号必须是 1..10，不能全是 1。"""
    patches = [
        {"fetch_timeout_seconds": 9.0},
        {"max_references": 9},
        {"max_redirects": 7},
        {"per_url_chars": 9000},
        {"min_chars": 500},
        {"min_total_chars": 1500},
        {"total_chars": 26000},
        {"total_ms": 9000},
        {"head_ratio": 0.6},
        {"max_file_bytes": 300_000},
    ]
    barrier = threading.Barrier(len(patches))

    def worker(item: dict[str, object]) -> int:
        barrier.wait()
        return cs.patch("input", item).version

    with ThreadPoolExecutor(max_workers=len(patches)) as pool:
        versions = sorted(pool.map(worker, patches))

    assert versions == list(range(1, len(patches) + 1))
    assert cs.resolved("input").version == len(patches)
    assert len({entry["version"] for entry in cs.audit("input", 50)}) == len(patches)


def test_conflict_is_raised_inside_transaction(conf: Path) -> None:
    """版本校验在事务内：拿到旧版本号的第二个写者必须 409，而不是静默覆盖。"""
    cs.patch("input", {"max_references": 7})
    with pytest.raises(cs.ConfigConflict) as caught:
        cs.patch("input", {"max_references": 8}, expected=99)
    assert caught.value.version == 1


# --- P1：drift / reset / persisted --------------------------------------


def test_drift_clears_after_reconfirming_new_default(conf: Path) -> None:
    """用户在新默认值上重新确认一次，drift 提示必须消失。"""
    cs.patch("health", {"latency_baseline_ms": 4000})
    with schema.connect() as conn:
        conn.execute(
            """UPDATE config_entries SET default_at_write='3000'
               WHERE domain='health' AND key='latency_baseline_ms'"""
        )
    cs.invalidate()
    assert cs.resolved("health").drifted == ["latency_baseline_ms"]

    cs.patch("health", {"latency_baseline_ms": 3500})
    assert cs.resolved("health").drifted == []


def test_reset_rejects_unknown_key(conf: Path) -> None:
    """`reset` 与 `patch` 同一套键校验：拼错不能静默成功。"""
    cs.patch("input", {"max_references": 9})
    with pytest.raises(cs.InvalidConfig, match="未知配置键"):
        cs.reset("input", ["max_referances"])
    assert cs.resolved("input").values["max_references"] == 9


def test_persisted_reflects_actual_write(conf: Path) -> None:
    """`persisted` 说的是「这次真的落库了」，不是「库里有版本号」。"""
    assert cs.patch("input", {"fetch_timeout_seconds": 5.0}).persisted is False
    assert cs.patch("input", {"max_references": 8}).persisted is True
    assert cs.reset("input", ["max_references"]).persisted is True
    # 全部清空后库里 0 行：再 reset 一次不再算写入
    assert cs.reset("input", all_=True).persisted is False


# --- P2：面板一次连接、入口配置拼错、policy 段 ---------------------------


def test_index_opens_one_connection(conf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """面板首屏不该为每个域各开一次连接、各跑一遍 DDL。"""
    # 先落一行覆盖，库文件才存在（否则 index 直接走「无库」分支，0 次连接也对）
    cs.patch("input", {"max_references": 9})
    calls = {"n": 0}
    original = schema.connect

    def spy(*args: object, **kwargs: object) -> Iterator[sqlite3.Connection]:
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(schema, "connect", spy)
    cs.index()
    assert calls["n"] == 1


def test_invalid_mode_is_rejected(conf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """拼错 `DEVOPS_AGENT_CONFIG_MODE` 不再静默当 file。"""
    monkeypatch.setenv(cs.MODE_ENV, "dbb")
    with pytest.raises(ValueError, match="不合法"):
        cs.mode()


def test_policy_file_is_read_as_root(conf: Path) -> None:
    """policy.yaml 的顶层就是 confirm 负载：顶层多一个键也不能让整段失效。"""
    (conf / "policy.yaml").write_text(
        "meta:\n  note: x\nconfirm:\n  display_limit: 1234\n", encoding="utf-8"
    )
    cs.invalidate()
    assert cs.resolved("policy").values["confirm"]["display_limit"] == 1234


def test_health_reports_effective_model_not_just_env(
    conf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/health` 的模型名以真正生效的解析结果为准（文件层压过环境变量）。"""
    from devops_agent.gateway.bootstrap import describe_runtime
    from devops_agent.model_settings import file_store

    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "env-model")
    file_store.save({"model": "file-model"})

    class _NotEcho:
        """非 Echo 即可：describe_runtime 只在 Echo 分支短路。"""

    assert describe_runtime(_NotEcho()).get("model") == "file-model"  # type: ignore[arg-type]


def test_export_uses_file_layout_for_root_domain(conf: Path) -> None:
    """root 域的导出不再包一层 `policy:`（贴回去会多一层）。"""
    text = cs.export_yaml("policy")
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert body == ["{}"]
    assert "policy:" not in text


def test_unknown_domain_still_raises(conf: Path) -> None:
    with pytest.raises(store.UnknownDomain):
        cs.resolved("nope")
