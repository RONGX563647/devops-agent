"""配置中心端点：域清单、快照、增量写、409、只读域、导出、审计。

用只挂本路由的极小 app，不装配运行时 —— 这些端点不该依赖模型凭据或会话注册表。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent import config as cfg
from devops_agent import config_store as cs
from devops_agent.config_store.api import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)


@pytest.fixture(autouse=True)
def conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    target = tmp_path / "config"
    shutil.copytree(cfg.config_dir(), target)
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(target))
    monkeypatch.setenv(cs.MODE_ENV, "db")
    monkeypatch.setenv(cs.CONFIG_DB_ENV, str(tmp_path / "config.db"))
    cfg.clear_cache()
    cs.invalidate()
    try:
        yield
    finally:
        cfg.clear_cache()
        cs.invalidate()


def test_index_lists_domains_and_storage() -> None:
    body = client.get("/api/config").json()
    assert body["mode"] == "db"
    assert body["db_path"].endswith("config.db")
    keys = [item["domain"] for item in body["domains"]]
    assert "input" in keys and "policy" in keys


def test_snapshot_has_values_sources_bounds() -> None:
    body = client.get("/api/config/input").json()
    assert body["values"]["max_references"] == 5
    # 部署 yaml 已删：没有覆盖时来源如实报 default，不再谎报 yaml 基线
    assert body["sources"]["max_references"] == "default"
    assert body["bounds"]["max_references"]["min"] == 1
    assert body["hot"] == "immediate"
    assert "fetch_timeout_seconds" in body["editable"]


def test_patch_writes_and_bumps_version() -> None:
    body = client.patch("/api/config/input", json={"patch": {"max_references": 9}}).json()
    assert body["values"]["max_references"] == 9
    assert body["sources"]["max_references"] == "db"
    assert body["version"] == 1


def test_patch_rejects_out_of_range() -> None:
    response = client.patch("/api/config/input", json={"patch": {"max_references": 0}})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "config_invalid"
    assert "max_references" in response.json()["detail"]["message"]


def test_patch_conflict_returns_current_snapshot() -> None:
    client.patch("/api/config/input", json={"patch": {"max_references": 9}})
    response = client.patch(
        "/api/config/input", json={"patch": {"max_references": 8}, "version": 99}
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "config_version_conflict"
    assert detail["version"] == 1


def test_readonly_domain_is_rejected() -> None:
    response = client.patch("/api/config/policy", json={"patch": {"confirm.display_limit": 3000}})
    assert response.status_code == 400
    assert "只读" in response.json()["detail"]["message"]


def test_reset_requires_explicit_scope() -> None:
    assert client.post("/api/config/input/reset", json={}).status_code == 400


def test_reset_keys_clears_only_those() -> None:
    client.patch("/api/config/input", json={"patch": {"max_references": 9, "max_redirects": 5}})
    body = client.post(
        "/api/config/input/reset", json={"keys": ["max_references"], "by": "tester"}
    ).json()
    assert body["values"]["max_references"] == 5
    assert body["values"]["max_redirects"] == 5
    assert body["version"] == 2


def test_export_returns_yaml() -> None:
    client.patch("/api/config/input", json={"patch": {"max_references": 9}})
    response = client.get("/api/config/input/export")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "max_references: 9" in response.text


def test_audit_lists_change() -> None:
    client.patch("/api/config/input", json={"patch": {"max_references": 9}, "by": "tester"})
    entries = client.get("/api/config/input/audit").json()["entries"]
    assert entries[0]["key"] == "max_references"
    assert entries[0]["by"] == "tester"


def test_unknown_domain_is_404() -> None:
    assert client.get("/api/config/nope").status_code == 404
