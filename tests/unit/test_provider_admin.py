"""网关写入口（SPEC-0022 §6）：校验、乐观锁、原子写、热加载。

这些用例断言的是「文件里到底写了什么」与「失败时文件有没有被动过」，
不是响应文案 —— 写入口的价值就在于它不撒谎。
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
from devops_agent.model_catalog import reset_catalog

client = TestClient(app)

BASE = """
providers:
  - id: dashscope
    kind: openai_chat
    label: 百炼
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: env:TEST_DASHSCOPE_KEY
    discover: manual
    models:
      - name: qwen-flash
  - id: gemini
    kind: gemini_chat
    label: Google
    base_url: https://generativelanguage.googleapis.com
    api_key: none
    discover: off
default: dashscope
"""


@pytest.fixture
def gateways(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    config_copy = tmp_path / "config"
    shutil.copytree(cfg.config_dir(), config_copy)
    for leftover in ("model.local.yaml", "models.local.yaml"):
        (config_copy / leftover).unlink(missing_ok=True)
    (config_copy / "providers.yaml").write_text(BASE, encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(config_copy))
    monkeypatch.setenv("TEST_DASHSCOPE_KEY", "sk-test-not-a-real-key")
    monkeypatch.delenv(ms.MODEL_ENV, raising=False)
    cfg.clear_cache()
    ms.invalidate()
    reset_catalog()
    app.state.sessions = None
    app.state.sessions_base = None
    try:
        yield config_copy
    finally:
        app.state.sessions = None
        app.state.sessions_base = None
        cfg.clear_cache()
        ms.invalidate()
        reset_catalog()


def _version() -> str:
    body = client.get("/api/providers").json()
    assert isinstance(body["version"], str)
    return str(body["version"])


def _provider(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "vllm-local",
        "kind": "openai_chat",
        "label": "内网 vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_ref": "VLLM_API_KEY",
        "discover": "manual",
    }
    body.update(over)
    return body


def test_list_exposes_content_version(gateways: Path) -> None:
    """版本是内容摘要：改 key 变量名也算变化，改注释不算。"""
    before = _version()
    path = gateways / "providers.yaml"
    path.write_text("# 手写注释\n" + BASE, encoding="utf-8")
    cfg.clear_cache()
    reset_catalog()
    assert _version() == before


def test_create_writes_file_and_is_visible_without_restart(gateways: Path) -> None:
    """A-4/A-12：保存后同一个进程立刻读得到，不重启。"""
    resp = client.post(
        "/api/providers",
        json={"expected_version": _version(), "provider": _provider(), "default": "dashscope"},
    )
    assert resp.status_code == 201, resp.text
    raw = yaml.safe_load((gateways / "providers.yaml").read_text(encoding="utf-8"))
    ids = [item["id"] for item in raw["providers"]]
    assert ids == ["dashscope", "gemini", "vllm-local"]
    assert raw["default"] == "dashscope"
    listed = client.get("/api/providers").json()
    assert [item["id"] for item in listed["items"]] == ids


def test_create_rejects_plaintext_key_and_leaves_file_untouched(gateways: Path) -> None:
    """A-5：明文 key 是唯一硬拦项，文件必须一个字节都不变。"""
    path = gateways / "providers.yaml"
    before = path.read_bytes()
    resp = client.post(
        "/api/providers",
        json={
            "expected_version": _version(),
            "provider": _provider(api_key_ref="sk-abcdefghijklmnop"),
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "plaintext_key"
    assert path.read_bytes() == before


def test_stale_version_conflicts(gateways: Path) -> None:
    """A-6：两个写者用同一版本，后到者 409 并拿到当前清单。"""
    version = _version()
    first = client.post(
        "/api/providers",
        json={"expected_version": version, "provider": _provider(), "default": "dashscope"},
    )
    assert first.status_code == 201
    second = client.patch(
        "/api/providers/gemini",
        json={
            "expected_version": version,
            "provider": _provider(id="gemini", kind="gemini_chat", api_key_ref="none"),
        },
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["code"] == "providers_version_conflict"
    assert detail["version"] != version


def test_patch_keeps_id_and_requires_version(gateways: Path) -> None:
    resp = client.patch(
        "/api/providers/gemini",
        json={
            "expected_version": _version(),
            "provider": _provider(id="renamed", kind="gemini_chat", api_key_ref="none"),
        },
    )
    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in client.get("/api/providers").json()["items"]]
    assert "gemini" in ids and "renamed" not in ids

    missing = client.patch("/api/providers/gemini", json={"provider": _provider()})
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "version_required"


def test_delete_default_requires_reassign(gateways: Path) -> None:
    """A-7：删默认网关必须指定接任者，否则会话会指向一个不存在的家。"""
    path = gateways / "providers.yaml"
    before = path.read_bytes()
    resp = client.delete(f"/api/providers/dashscope?expected_version={_version()}")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "reassign_required"
    assert path.read_bytes() == before

    ok = client.delete(f"/api/providers/dashscope?expected_version={_version()}&reassign_to=gemini")
    assert ok.status_code == 200, ok.text
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["default"] == "gemini"


def test_unknown_kind_is_rejected(gateways: Path) -> None:
    resp = client.post(
        "/api/providers",
        json={"expected_version": _version(), "provider": _provider(kind="not_a_kind")},
    )
    assert resp.status_code == 400
    assert "kind" in str(resp.json()["detail"])


def test_check_does_not_touch_file(gateways: Path) -> None:
    path = gateways / "providers.yaml"
    before = path.read_bytes()
    resp = client.post("/api/providers/gemini/check")
    assert resp.status_code == 200
    assert resp.json()["code"] == "ok"
    assert path.read_bytes() == before
