"""模型发现（SPEC-0018 §5）：协议族、缓存、失败矩阵、超时。

全部走 `httpx.MockTransport`：这层要验的是「上游怎么说，我们怎么归类」，
而不是真实网关通不通。真实端点尚未实测，spec §5.2 已如实标注。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from devops_agent.model_catalog import cache as catalog_cache
from devops_agent.model_catalog.discover import TIMEOUT, discover, set_transport
from devops_agent.model_catalog.normalize import normalize
from devops_agent.model_catalog.schema import ProviderSpec

OPENAI_SPEC = ProviderSpec(
    id="prov", kind="openai_chat", base_url="https://gw.example.com/v1", api_key="none"
)


@pytest.fixture(autouse=True)
def _reset_transport() -> None:
    yield
    set_transport(lambda: None)


def _use(handler) -> list[httpx.Request]:
    """装一个假网关并返回请求记录（用来断言「第二次没发请求」）。"""
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    set_transport(lambda: httpx.MockTransport(record))
    return calls


def _run(spec: ProviderSpec, *, force: bool = False, cache=None) -> object:
    return asyncio.run(discover(spec, force=force, cache=cache))


def _cache(tmp_path: Path) -> catalog_cache.CatalogCache:
    return catalog_cache.CatalogCache(tmp_path)


# --- 归一化：五家形状 --------------------------------------------------


def test_normalize_openai_window_synonyms() -> None:
    """窗口字段同义映射：vLLM / OpenRouter / Groq / Mistral 各叫各的。"""
    cases = [
        ({"max_model_len": 32768}, 32768),
        ({"context_length": 131072}, 131072),
        ({"context_window": 8192}, 8192),
        ({"max_context_length": 65536}, 65536),
        ({"top_provider": {"context_length": 16000}}, 16000),
    ]
    for raw, expected in cases:
        items = normalize({"data": [{"id": "m", **raw}]}, "openai")
        assert items is not None
        assert items[0].context_size == expected


def test_normalize_openrouter_modalities() -> None:
    """`text`/`image`/`file` → MIME 粗分类；没声明就是空元组（不是默认纯文本）。"""
    items = normalize(
        {"data": [{"id": "m", "architecture": {"input_modalities": ["text", "image"]}}]},
        "openai",
    )
    assert items is not None
    assert items[0].input_types == ("text/plain", "image/*")

    plain = normalize({"data": [{"id": "m"}]}, "openai")
    assert plain is not None
    assert plain[0].input_types == ()


def test_normalize_gemini_strips_prefix_and_filters() -> None:
    """Gemini 的 id 带 `models/` 前缀，且非 generateContent 的模型要滤掉。"""
    payload = {
        "models": [
            {
                "name": "models/gemini-2.5-pro",
                "inputTokenLimit": 1048576,
                "outputTokenLimit": 65536,
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/text-embedding-004",
                "supportedGenerationMethods": ["embedContent"],
            },
        ]
    }
    items = normalize(payload, "gemini")
    assert items is not None
    assert [item.id for item in items] == ["gemini-2.5-pro"]
    assert items[0].context_size == 1048576
    assert items[0].output_size == 65536


def test_normalize_ollama_and_anthropic() -> None:
    ollama = normalize({"models": [{"name": "llama3:8b", "modified_at": "2026-01-01"}]}, "ollama")
    assert ollama is not None
    assert ollama[0].id == "llama3:8b"
    assert ollama[0].context_size is None  # 要 /api/show，本期取不到

    anthropic = normalize(
        {
            "data": [
                {"id": "claude-x", "display_name": "Claude X", "created_at": "2026-01-01T00:00:00Z"}
            ]
        },
        "anthropic",
    )
    assert anthropic is not None
    assert anthropic[0].label == "Claude X"


def test_normalize_unknown_shape_returns_none() -> None:
    """形状不认识必须返回 None（→ malformed），不能当空列表 —— 否则「网关报错」
    会显示成「没有任何模型」。"""
    assert normalize({"error": "boom"}, "openai") is None
    assert normalize([], "openai") is None
    assert normalize({"data": "nope"}, "openai") is None


# --- A36 缓存 ---------------------------------------------------------


def test_second_call_does_not_hit_http(tmp_path: Path) -> None:
    """A36：缓存命中时第二次不发 HTTP，且 items 带 id/label。"""
    calls = _use(
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "m1", "display_name": "Model One"}, {"id": "m2"}]},
            headers={"etag": 'W/"v1"'},
        )
    )
    store = _cache(tmp_path)
    first = _run(OPENAI_SPEC, cache=store)
    second = _run(OPENAI_SPEC, cache=store)

    assert len(calls) == 1
    assert first.code == "ok"
    assert {item.id for item in first.items} == {"m1", "m2"}
    assert second.code == "ok"
    assert second.items == first.items


def test_force_bypasses_cache(tmp_path: Path) -> None:
    """用户点刷新：`force=True` 必须真的再打一次。"""
    calls = _use(lambda request: httpx.Response(200, json={"data": [{"id": "m"}]}))
    store = _cache(tmp_path)
    _run(OPENAI_SPEC, cache=store)
    _run(OPENAI_SPEC, cache=store, force=True)
    assert len(calls) == 2


def test_etag_304_refreshes_timestamp_without_new_data(tmp_path: Path) -> None:
    """304：数据不动，只刷新 `fetched_at`，且不清空列表。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, json={"data": [{"id": "m"}]}, headers={"etag": 'W/"v1"'})

    _use(handler)
    store = _cache(tmp_path)
    first = _run(OPENAI_SPEC, cache=store)
    second = _run(OPENAI_SPEC, cache=store, force=True)
    assert second.code == "ok"
    assert [item.id for item in second.items] == [item.id for item in first.items]


def test_new_key_invalidates_cache(tmp_path: Path) -> None:
    """缓存键含 key 指纹：换 key 必须重取（不同 key 可见模型集可能不同）。"""
    calls = _use(lambda request: httpx.Response(200, json={"data": [{"id": "m"}]}))
    store = _cache(tmp_path)
    _run(OPENAI_SPEC, cache=store)
    other_key = ProviderSpec(
        id="prov", kind="openai_chat", base_url="https://gw.example.com/v1", api_key="sk-other"
    )
    _run(other_key, cache=store)
    assert len(calls) == 2


def test_disk_cache_keeps_only_identity(tmp_path: Path) -> None:
    """落盘只存 id/label/created_at：窗口落盘会产生第二份真相。"""
    _use(lambda request: httpx.Response(200, json={"data": [{"id": "m", "context_length": 999}]}))
    store = _cache(tmp_path)
    _run(OPENAI_SPEC, cache=store)

    on_disk = json.loads((tmp_path / "prov.json").read_text(encoding="utf-8"))
    assert set(on_disk["items"][0]) == {"id", "label", "created_at"}

    cold = catalog_cache.CatalogCache(tmp_path)
    restored = cold.from_disk("prov")
    assert restored is not None
    assert restored.items[0].id == "m"
    assert restored.items[0].context_size is None
    assert restored.stale is True


# --- A37 401 不重试 ---------------------------------------------------


def test_unauthorized_does_not_retry_and_is_negatively_cached(tmp_path: Path) -> None:
    """A37：401 只打一次，30s 内再问也不发；有旧缓存时回旧值并标 stale。"""
    calls = _use(lambda request: httpx.Response(401, json={"error": "bad key"}))
    store = _cache(tmp_path)
    first = _run(OPENAI_SPEC, cache=store)
    second = _run(OPENAI_SPEC, cache=store)

    assert first.code == "unauthorized"
    assert len(calls) == 1
    assert second.code == "unauthorized"


def test_failure_falls_back_to_stale_cache(tmp_path: Path) -> None:
    """有旧列表时 500 不该让面板空白：回旧值 + `stale=True` + 失败码。"""
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    _use(handler)
    store = _cache(tmp_path)
    _run(OPENAI_SPEC, cache=store)
    state["fail"] = True
    result = _run(OPENAI_SPEC, cache=store, force=True)
    assert result.code == "unavailable"
    assert result.stale is True
    assert [item.id for item in result.items] == ["m"]


# --- 失败语义矩阵 ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "unsupported"),
        (429, "rate_limited"),
        (500, "unavailable"),
        (503, "unavailable"),
        (302, "malformed"),  # 不跟随重定向：会把 key 带到另一个主机
        (400, "malformed"),
    ],
)
def test_status_matrix(tmp_path: Path, status: int, code: str) -> None:
    _use(lambda request: httpx.Response(status))
    result = _run(OPENAI_SPEC, cache=_cache(tmp_path))
    assert result.code == code
    assert result.http_status == status


def test_empty_and_malformed_are_distinguished(tmp_path: Path) -> None:
    """空列表与形状不认识必须分开：前者是「网关没模型」，后者是「我们不认识」。"""
    _use(lambda request: httpx.Response(200, json={"data": []}))
    assert _run(OPENAI_SPEC, cache=_cache(tmp_path)).code == "empty"

    _use(lambda request: httpx.Response(200, json={"unexpected": True}))
    assert _run(OPENAI_SPEC, cache=_cache(tmp_path / "b")).code == "malformed"


def test_not_configured_sends_nothing(tmp_path: Path) -> None:
    """缺 key 的 provider 不发请求（A47：缺 key 不阻止别人，但自己别打空炮）。"""
    calls = _use(lambda request: httpx.Response(200, json={"data": []}))
    broken = ProviderSpec(
        id="b", kind="openai_chat", base_url="https://h/v1", api_key="env:DEFINITELY_MISSING"
    )
    result = _run(broken, cache=_cache(tmp_path))
    assert result.code == "not_configured"
    assert calls == []


# --- A43 超时 ---------------------------------------------------------


def test_timeout_becomes_unavailable(tmp_path: Path) -> None:
    """A43：超时被吞成 `unavailable` 并回卡，绝不冒泡到调用方。

    这里不假装测了「等 3 秒」：真实计时靠 `TIMEOUT` 的两个值，断言它固定，
    免得有人为了「快一点」把它改成 0.5s 而在慢网关上误判成网关坏了。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _use(handler)
    result = _run(OPENAI_SPEC, cache=_cache(tmp_path))
    assert result.code == "unavailable"


def test_timeout_budget_is_pinned() -> None:
    """A43 的另一半：超时预算是显式契约，不是随手写的默认值。"""
    assert TIMEOUT.connect == 2.0
    assert TIMEOUT.read == 3.0


def test_request_is_not_redirect_friendly() -> None:
    """3xx 归 malformed 的前提是「不跟随」；跟随了 key 就去别的主机了。"""
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(True)
        return httpx.Response(302, headers={"location": "https://evil.example.com/models"})

    _use(handler)
    result = _run(OPENAI_SPEC, cache=catalog_cache.CatalogCache())
    assert result.code == "malformed"
    assert seen == [True]
