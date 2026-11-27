"""供应商目录端点：脱敏（A41）、能力卡对写路径的二次约束（A46）、自述（A47）。

不发真实模型请求：这里验的是「服务端是不是唯一裁判」与「响应里有没有不该有的东西」，
不是网关回什么。发现类请求一律用 `discover: manual` / `off` 绕开，需要打上游的
用例在 `test_model_catalog_discover.py` 里用 MockTransport 单独覆盖。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent import config as cfg
from devops_agent import model_settings as ms
from devops_agent.gateway.app import app
from devops_agent.model_catalog import reset_catalog
from devops_agent.observability import repository as repo

client = TestClient(app)

#: 两把假 key：断言的是「有没有原样出现在响应里」，不是它们的强度。
DASHSCOPE_KEY = "sk-bailian-7f3c9a1e5d2b"
GEMINI_KEY = "AIzaSy-b8e1d4c7a0f3"

#: 三种 discover 各来一个：
#: - dashscope 是人工清单（不打上游），用真实卡 qwen-flash 验 schema；
#: - gemini 是 off（纯离线列卡）；
#: - nokey 地址齐全但没有 key，用来验「一个人缺 key 不拖累别人」。
PROVIDERS = """
providers:
  - id: dashscope
    kind: dashscope_chat
    label: 百炼
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: env:TEST_DASHSCOPE_KEY
    discover: manual
    models:
      - name: qwen-flash
      - name: qwen-plus
  - id: gemini
    kind: gemini_chat
    label: Google
    base_url: https://generativelanguage.googleapis.com
    api_key: env:TEST_GEMINI_KEY
    discover: off
  - id: nokey
    kind: openai_chat
    label: 未配 key 的网关
    base_url: https://gw.example.com/v1
    api_key: env:TEST_ABSENT_KEY
    discover: off
"""


def _rewrite(directory: Path, text: str) -> None:
    """改 providers.yaml 并让进程立刻读到新的一份。"""
    (directory / "providers.yaml").write_text(text, encoding="utf-8")
    cfg.clear_cache()
    ms.invalidate()
    reset_catalog()


@pytest.fixture
def gateways(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """整目录拷到 tmp：PATCH 会写 model.local.yaml，不能污染仓库的 config/。"""
    config_copy = tmp_path / "config"
    shutil.copytree(cfg.config_dir(), config_copy)
    for leftover in ("model.local.yaml", "models.local.yaml"):
        (config_copy / leftover).unlink(missing_ok=True)
    (config_copy / "providers.yaml").write_text(PROVIDERS, encoding="utf-8")

    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(config_copy))
    monkeypatch.setenv(repo.DB_PATH_ENV, str(tmp_path / "catalog.db"))
    monkeypatch.setenv("TEST_DASHSCOPE_KEY", DASHSCOPE_KEY)
    monkeypatch.setenv("TEST_GEMINI_KEY", GEMINI_KEY)
    monkeypatch.delenv("TEST_ABSENT_KEY", raising=False)
    # 环境变量里的模型名会盖掉 yaml，把「未显式选模型」这个前提打掉
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


def _text(resp: object) -> str:
    """响应体的原始文本：脱敏断言要扫全文，不能只看某几个键。"""
    return json.dumps(resp.json(), ensure_ascii=False)  # type: ignore[attr-defined]


# --- 脱敏（A41） --------------------------------------------------------


def test_providers_payload_never_leaks_credentials(gateways: Path) -> None:
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body["items"]] == ["dashscope", "gemini", "nokey"]
    assert body["anonymous"] is False
    assert body["default"] == "dashscope"

    text = _text(resp)
    for secret in (DASHSCOPE_KEY, GEMINI_KEY):
        # 明文与后缀都不能出现：后缀也是信息，够拼出「是不是同一把」
        assert secret not in text
        assert secret[-6:] not in text
    # base_url 只回 host：完整 URL 上可能挂着 token 查询参数
    assert "compatible-mode" not in text
    dashscope = body["items"][0]
    assert dashscope["base_url_host"] == "dashscope.aliyuncs.com"
    assert dashscope["has_key"] is True
    assert len(dashscope["key_fingerprint"]) == 8
    assert set(dashscope["key_fingerprint"]) <= set("0123456789abcdef")
    # 缺 key 的说「没有」，而不是悄悄给一个空字符串当指纹
    assert body["items"][2]["has_key"] is False
    assert body["items"][2]["key_fingerprint"] == ""


def test_key_fingerprint_stable_then_changes(gateways: Path) -> None:
    """同一把 key 稳定、换 key 变化：面板靠它判断「配置是不是被改过」。"""
    before = client.get("/api/providers").json()["items"]
    # 两处用同一把 key（同一 env 变量）→ 指纹必须相同，说明它只跟 key 走
    same_key = PROVIDERS.replace("api_key: env:TEST_GEMINI_KEY", "api_key: env:TEST_DASHSCOPE_KEY")
    _rewrite(gateways, same_key)
    shared = client.get("/api/providers").json()["items"]
    assert (
        shared[0]["key_fingerprint"] == shared[1]["key_fingerprint"] == before[0]["key_fingerprint"]
    )

    # 换一把 key → 指纹变；但不该变的东西（host、id）不许跟着变
    changed = same_key.replace("api_key: env:TEST_DASHSCOPE_KEY", "api_key: env:TEST_GEMINI_KEY")
    _rewrite(gateways, changed)
    after = client.get("/api/providers").json()["items"]
    assert after[0]["key_fingerprint"] != before[0]["key_fingerprint"]
    assert after[0]["base_url_host"] == before[0]["base_url_host"]


# --- 能力卡与参数上限（A46） --------------------------------------------


def test_schema_caps_max_tokens(gateways: Path) -> None:
    """qwen-flash 的输出上限是 32768：BOUNDS 只写 min，卡负责收紧。"""
    resp = client.patch(
        "/api/model-config",
        json={"patch": {"provider": "dashscope", "model": "qwen-flash", "max_tokens": 99_999}},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_model_config"
    assert detail["field"] == "max_tokens"
    # 文案必须带上限，否则用户只知道被拒、不知道改成多少
    assert "32768" in detail["reason"]
    assert detail["bounds"]["kind"] == "int"

    ok = client.patch(
        "/api/model-config",
        json={"patch": {"provider": "dashscope", "model": "qwen-flash", "max_tokens": 32_768}},
    )
    assert ok.status_code == 200
    assert ok.json()["effective"]["max_tokens"] == 32_768


def test_bounds_still_win_over_schema(gateways: Path) -> None:
    """schema 只能收紧，BOUNDS 也照样拦：关掉跟随后手填的窗口仍受硬下限约束。"""
    resp = client.patch(
        "/api/model-config",
        json={
            "patch": {
                "provider": "dashscope",
                "model": "qwen-flash",
                "context_auto": False,
                "context_size": 4096,
            }
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["field"] == "context_size"
    assert detail["bounds"]["min"] == 8192


def test_context_auto_ignores_hand_typed_window(gateways: Path) -> None:
    """跟随模型能力时，手填的窗口不生效 —— 这是 A39 的正面行为。

    顺序在 `resolve` 里：能力合并发生在存储层之后，所以「用户填了 65536、
    实际按 100 万算」是刻意的。不这样，换模型后窗口永远停在旧值上。
    """
    body = client.patch(
        "/api/model-config",
        json={"patch": {"provider": "dashscope", "model": "qwen-flash", "context_size": 65_536}},
    ).json()
    assert body["effective"]["context_size"] == 1_000_000
    assert body["sources"]["context_size"] == "card"
    assert body["catalog"]["current"]["context_source"] == "card"


def test_unknown_provider_rejected_by_registry(gateways: Path) -> None:
    resp = client.patch("/api/model-config", json={"patch": {"provider": "ghost"}})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["field"] == "provider"
    assert "ghost" in detail["reason"]


def test_unmatched_model_warns_instead_of_failing(gateways: Path) -> None:
    """网关的 /models 常不全，手填模型名是合法用法：只提示「未校准」。"""
    resp = client.patch(
        "/api/model-config",
        json={"patch": {"provider": "dashscope", "model": "qwen-max-latest"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any("未在内置卡中找到" in item for item in body["warnings"])
    assert body["catalog"]["current"]["matched_by"] == "none"


# --- 自述（A47） --------------------------------------------------------


def test_health_reports_provider_catalog_and_missing_key(gateways: Path) -> None:
    model = client.get("/health").json()["model"]
    # 未显式选网关时自述注册表默认项，而不是一个叫 default 的幻影
    assert model["provider"] == "dashscope"
    assert model["kind"] == "dashscope_chat"
    assert model["catalog"]["providers"] == 3
    assert model["catalog"]["missing_key"] == ["nokey"]
    assert model["catalog"]["enabled"] is True
    assert model["catalog"]["anonymous"] is False


def test_missing_key_does_not_block_others(gateways: Path) -> None:
    """nokey 缺 key 只进 missing_key；它旁边的网关照样能查出能力卡。"""
    listed = client.get("/api/providers").json()["items"]
    assert [item["id"] for item in listed if not item["has_key"]] == ["nokey"]
    assert client.get("/health").json()["model"]["catalog"]["missing_key"] == ["nokey"]

    client.patch(
        "/api/model-config",
        json={"patch": {"provider": "dashscope", "model": "qwen-flash"}},
    )
    body = client.get("/api/model-config").json()
    assert body["effective"]["context_size"] == 1_000_000
    assert body["catalog"]["current"]["status"] == "active"


# --- 面板需要的上下文 ---------------------------------------------------


def test_model_config_exposes_catalog_block(gateways: Path) -> None:
    client.patch(
        "/api/model-config",
        json={"patch": {"provider": "dashscope", "model": "qwen-flash"}},
    )
    body = client.get("/api/model-config").json()
    catalog = body["catalog"]
    assert catalog["providers_configured"] == 3
    assert catalog["provider"] == "dashscope"
    assert catalog["current"]["matched_by"] == "exact"
    # 人工清单只有名字、没有窗口 → 窗口来自内置卡
    assert catalog["current"]["context_source"] == "card"
    # 来自卡的窗口可能滞后，界面要能提示「这个数字可能过期」
    assert catalog["current"]["card_may_be_stale"] is True
    assert body["derived"]["context_source"] == "card"
    assert body["derived"]["context_uncalibrated"] is False


def test_context_auto_defaults_true_when_registry_present(gateways: Path) -> None:
    """写了 providers.yaml 就进入「跟随模型能力」模式（A39 的前提）。"""
    body = client.get("/api/model-config").json()
    assert body["effective"]["context_auto"] is True
    assert body["sources"]["context_auto"] == "framework-default"


# --- 列表端点 -----------------------------------------------------------


def test_manual_provider_lists_without_upstream(gateways: Path) -> None:
    resp = client.get("/api/providers/dashscope/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "ok"
    assert body["total"] == 2
    item = next(item for item in body["items"] if item["id"] == "qwen-flash")
    # 列表项直接带上合并后的窗口与匹配方式，面板不必逐个再请求一次
    assert item["context_size"] == 1_000_000
    assert item["matched_by"] == "exact"


def test_off_provider_lists_builtin_cards_and_refresh_is_not_error(gateways: Path) -> None:
    """`discover: off` 没有上游可打，刷新等于重列一遍，不该报错。"""
    body = client.get("/api/providers/gemini/models").json()
    assert body["code"] == "ok"
    assert body["total"] > 0
    assert client.post("/api/providers/gemini/models/refresh").status_code == 200


def test_unknown_provider_404_lists_available(gateways: Path) -> None:
    resp = client.get("/api/providers/ghost/models")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "unknown_provider"
    # 回可用列表：前端据此把下拉切到正确的那个，而不是只弹一句错误
    assert detail["available"] == ["dashscope", "gemini", "nokey"]


def test_model_id_path_parameter_is_not_truncated(gateways: Path) -> None:
    """Gemini 的模型 id 自带前缀，普通路径参数会在 `/` 处截断成 models。"""
    resp = client.get("/api/providers/gemini/models/models/gemini-2.5-pro")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "gemini"
    assert body["model"] == "models/gemini-2.5-pro"
    assert body["capability"]["context_size"] > 0
