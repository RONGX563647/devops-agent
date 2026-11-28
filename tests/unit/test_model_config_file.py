"""`config/model.local.yaml` 落盘层：优先级、手改生效、迁移、写失败要吵。

这组测试盯的是这次修复的核心缺陷：界面改的值原先只落在会话或 DB，
换会话/换机器就消失。所以每一项都断言「文件里到底是什么」，
而不是只断言接口返回了什么。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from devops_agent import config as cfg
from devops_agent import model_settings as ms
from devops_agent.gateway.app import app
from devops_agent.observability import repository as repo

client = TestClient(app)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """整目录隔离：不只隔离 DB，也隔离 config/，否则测试会写进仓库。"""
    target = tmp_path / "config"
    shutil.copytree(cfg.config_dir(), target)
    (target / "model.local.yaml").unlink(missing_ok=True)
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(target))
    monkeypatch.setenv(repo.DB_PATH_ENV, str(tmp_path / "obs.db"))
    monkeypatch.delenv(ms.MODEL_ENV, raising=False)
    cfg.clear_cache()
    ms.invalidate()
    app.state.sessions = None
    app.state.sessions_base = None
    try:
        yield target
    finally:
        cfg.clear_cache()
        ms.invalidate()
        app.state.sessions = None
        app.state.sessions_base = None


def _file_values(config_dir: Path) -> dict[str, object]:
    raw = yaml.safe_load((config_dir / "model.local.yaml").read_text(encoding="utf-8"))
    return raw.get("model", raw)


def test_patch_writes_file_and_keeps_header(config_dir: Path) -> None:
    body = client.patch("/api/model-config", json={"patch": {"temperature": 0.3}}).json()
    assert body["effective"]["temperature"] == 0.3
    assert body["sources"]["temperature"] == "stored"
    assert body["file_path"].endswith("model.local.yaml")

    path = config_dir / "model.local.yaml"
    assert path.is_file()
    assert _file_values(config_dir) == {"temperature": 0.3}
    # 注释头是给人看的说明书：重写不能把它丢掉
    assert path.read_text(encoding="utf-8").startswith("# 由界面")


def test_file_beats_stale_db_value(config_dir: Path) -> None:
    """文件是真相：DB 里残留的旧值不能盖掉手改过的文件。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        repo.save_setting(
            conn,
            ms.SETTING_KEY,
            {"temperature": 0.9},
            expected_version=None,
            updated_at="2026-11-26T00:00:00+00:00",
        )
    (config_dir / "model.local.yaml").write_text(
        "model:\n  temperature: 0.2\n", encoding="utf-8", newline="\n"
    )
    ms.invalidate()
    body = client.get("/api/model-config").json()
    assert body["effective"]["temperature"] == 0.2


def test_hand_edit_takes_effect_without_restart(config_dir: Path) -> None:
    """手改文件后刷新即生效：mtime 失效不需要重启进程。"""
    client.patch("/api/model-config", json={"patch": {"max_iters": 30}})
    assert client.get("/api/model-config").json()["effective"]["max_iters"] == 30

    (config_dir / "model.local.yaml").write_text(
        "model:\n  max_iters: 42\n", encoding="utf-8", newline="\n"
    )
    body = client.get("/api/model-config").json()
    assert body["effective"]["max_iters"] == 42


def test_old_db_only_deployment_is_migrated_on_read(config_dir: Path) -> None:
    """老部署只有 DB 值：第一次读配置就把它落盘，之后才谈得上可 diff。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        repo.save_setting(
            conn,
            ms.SETTING_KEY,
            {"max_iters": 120},
            expected_version=None,
            updated_at="2026-11-26T00:00:00+00:00",
        )
    ms.invalidate()
    body = client.get("/api/model-config").json()
    assert body["effective"]["max_iters"] == 120
    assert _file_values(config_dir) == {"max_iters": 120}


def test_reset_removes_key_from_file(config_dir: Path) -> None:
    client.patch("/api/model-config", json={"patch": {"temperature": 0.3, "max_iters": 30}})
    body = client.post("/api/model-config/reset", json={"keys": ["temperature"]}).json()
    assert body["effective"]["temperature"] is None
    assert _file_values(config_dir) == {"max_iters": 30}

    body = client.post("/api/model-config/reset", json={"all": True}).json()
    assert body["effective"]["max_iters"] == 12
    assert _file_values(config_dir) == {}


def test_layers_expose_every_source(config_dir: Path) -> None:
    client.patch("/api/model-config", json={"patch": {"temperature": 0.3}})
    client.patch("/api/session/s9/model-config", json={"overrides": {"top_p": 0.5}})
    ms.invalidate()
    body = client.get("/api/model-config/layers", params={"session": "s9"}).json()

    assert body["file_path"].endswith("model.local.yaml")
    layers = {item["id"]: item for item in body["layers"]}
    assert layers["session"]["values"] == {"top_p": 0.5}
    assert layers["file"]["values"] == {"temperature": 0.3}
    assert layers["file"]["writable"] is True
    assert layers["base"]["writable"] is False
    # 生效值仍按「会话 > 文件 > 基础」合成
    assert body["effective"]["top_p"] == 0.5
    assert body["effective"]["temperature"] == 0.3
    assert body["sources"]["top_p"] == "session"


def test_unwritable_target_reports_failure(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写不进去必须 500，不能返回 200 让人以为存上了。"""

    def boom(_values: dict[str, object]) -> Path:
        raise OSError("disk is read-only")

    monkeypatch.setattr(ms, "save_local", boom)
    resp = client.patch("/api/model-config", json={"patch": {"temperature": 0.3}})
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "model_config_not_persisted"
    assert "model.local.yaml" in detail["reason"]
