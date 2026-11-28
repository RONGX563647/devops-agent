"""模型发现：按协议族打上游 `/models`，永不抛异常。

只有三处触发（设置面板、手动刷新、`/health` 自检），对话链路完全不碰它。
所有失败都归到一个 `code`，调用方按码出文案 —— 让 UI 去猜 `None` 的含义，
就是「面板空白但没人知道为什么」的来源。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from devops_agent.model_catalog.cache import CatalogCache, shared
from devops_agent.model_catalog.kinds import kind_of
from devops_agent.model_catalog.normalize import normalize
from devops_agent.model_catalog.schema import DiscoveryItem, DiscoveryResult, ProviderSpec

logger = logging.getLogger(__name__)

#: 连接 2s：内网网关通常 <100ms，2s 已是异常；读 3s：模型列表在慢网关可达 2s+。
TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)

#: 面板首屏会并发拉多个 provider；再高意义不大，网关自己会限流。
MAX_CONCURRENCY = 4

#: 上游返回体进日志时的截断长度：全文可能几 MB，而且可能含敏感串。
BODY_SNIPPET = 200

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)


#: 测试注入点：给一个返回 `httpx.AsyncBaseTransport` 的工厂，就能让发现走假网关。
#: 生产恒为 None（默认 transport）。刻意不做成参数：那会把测试用的形状泄漏到
#: 每个调用点，而这里只需要在边界上换一次实现。
def _no_transport() -> httpx.AsyncBaseTransport | None:
    """生产默认：用 httpx 自己的 transport。"""
    return None


_transport_factory: Callable[[], httpx.AsyncBaseTransport | None] = _no_transport


def set_transport(factory: Callable[[], httpx.AsyncBaseTransport | None]) -> None:
    """替换底层 transport（测试用）。传回 `lambda: None` 即恢复默认。"""
    global _transport_factory
    _transport_factory = factory


CacheKey = tuple[str, str, str]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _api_url(base: str, path: str) -> str:
    """拼路径时避免 `/v1/v1`：base 已带版本号就不再重复。"""
    base = base.rstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        return base + path[len("/v1") :]
    return base + path


def request_parts(spec: ProviderSpec) -> tuple[str, dict[str, str], dict[str, str]]:
    """按协议族返回 (url, headers, params)。

    Gemini 用 `x-goog-api-key` 头而不是 `?key=`：key 进 URL 就会被访问日志、
    代理与浏览器历史记下来，而官方头是等价支持的。
    """
    family = kind_of(spec).family
    base = spec.base_url.rstrip("/")
    key = spec.resolved_key or ""
    if family == "ollama":
        return f"{base}/api/tags", {}, {}
    if family == "gemini":
        return _api_url(base, "/models"), {"x-goog-api-key": key}, {}
    if family == "anthropic":
        return (
            _api_url(base, "/v1/models"),
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {},
        )
    return _api_url(base, "/models"), {"Authorization": f"Bearer {key}"}, {}


def _code_for(status: int) -> str:
    """HTTP 状态 → 归一码。3xx 归 malformed：跟随重定向会把 key 带到另一个主机。"""
    if status in {401, 403}:
        return "unauthorized"
    if status == 404:
        return "unsupported"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "unavailable"
    return "malformed"


def _payload_code(items: list[DiscoveryItem] | None) -> str:
    if items is None:
        return "malformed"
    return "ok" if items else "empty"


def _snippet(text: str) -> str:
    """响应片段截断：只用于排障，不整段落日志。"""
    return text[:BODY_SNIPPET].replace("\n", " ")


async def _request(spec: ProviderSpec, headers: dict[str, str]) -> httpx.Response:
    url, base_headers, params = request_parts(spec)
    merged = {**base_headers, **headers}
    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=False, transport=_transport_factory()
    ) as client:
        return await client.get(url, headers=merged, params=params)


def _failure(spec: ProviderSpec, response: httpx.Response) -> DiscoveryResult:
    """非 200 一律转成码：调用方按 `code` 出文案，不靠解析异常。"""
    code = _code_for(response.status_code)
    if code == "malformed":
        logger.warning(
            "模型列表形状异常 %s：HTTP %s %s",
            spec.id,
            response.status_code,
            _snippet(response.text),
        )
    return DiscoveryResult(code=code, fetched_at=now_iso(), http_status=response.status_code)


def _revalidated(spec: ProviderSpec, cache: CatalogCache, key: CacheKey) -> DiscoveryResult | None:
    """304 命中：数据没变，只刷新时间戳（否则面板会一直显示「10 分钟前」）。

    本地没有缓存时返回 None —— 那是「缓存被清过」，退化成一次普通请求，不该报错。
    """
    cached = cache.fresh(key) or cache.older(spec.id, key)
    if cached is None:
        return None
    return replace(cached, code="ok", fetched_at=now_iso(), stale=False)


def _log_payload(spec: ProviderSpec, code: str, response: httpx.Response) -> None:
    if code == "malformed":
        logger.warning("模型列表形状不认识：%s %s", spec.id, _snippet(response.text))
    elif code == "empty":
        logger.info("网关 %s 未返回任何模型", spec.id)


def _success(
    spec: ProviderSpec, cache: CatalogCache, key: CacheKey, response: httpx.Response
) -> DiscoveryResult:
    status = response.status_code
    try:
        payload: Any = response.json()
    except ValueError:
        logger.warning("模型列表不是 JSON：%s %s", spec.id, _snippet(response.text))
        return DiscoveryResult(code="malformed", fetched_at=now_iso(), http_status=status)
    items = normalize(payload, kind_of(spec).family)
    code = _payload_code(items)
    _log_payload(spec, code, response)
    # 只在成功时记 ETag：对 malformed/empty 记 ETag 会让下一次条件请求
    # 命中 304，于是坏响应被永久缓存，用户点了刷新也拿不到新数据
    if code == "ok":
        cache.set_etag(key, response.headers.get("etag", ""))
    return DiscoveryResult(
        items=tuple(items or ()), code=code, fetched_at=now_iso(), http_status=status
    )


async def _fetch(spec: ProviderSpec, cache: CatalogCache, key: CacheKey) -> DiscoveryResult:
    """一次发现请求：条件请求 → 200 归一化 / 非 200 转码。"""
    etag = cache.etag(key)
    response = await _request(spec, {"If-None-Match": etag} if etag else {})
    if response.status_code == 304:
        revalidated = _revalidated(spec, cache, key)
        if revalidated is not None:
            return revalidated
        response = await _request(spec, {})
    if response.status_code != 200:
        return _failure(spec, response)
    return _success(spec, cache, key, response)


async def discover(
    spec: ProviderSpec, *, force: bool = False, cache: CatalogCache | None = None
) -> DiscoveryResult:
    """拉一次模型列表。不抛异常：任何失败都变成 `DiscoveryResult.code`。

    `force=True` 绕过缓存（用户点了刷新）；仍然写回缓存，下一次就不必再打。
    """
    store = cache if cache is not None else shared()
    if not spec.configured:
        return DiscoveryResult(code="not_configured", fetched_at=now_iso())

    key: CacheKey = (spec.id, spec.base_url, spec.key_fingerprint)
    if not force:
        hit = store.fresh(key)
        if hit is not None:
            return hit

    try:
        async with _semaphore:
            result = await _fetch(spec, store, key)
    except httpx.HTTPError as exc:
        # 超时 / 连接失败 / 协议错误：都归 unavailable，交给调用方回旧缓存
        logger.warning("模型发现失败 %s：%s: %s", spec.id, type(exc).__name__, exc)
        result = DiscoveryResult(code="unavailable", fetched_at=now_iso())
    except Exception:  # noqa: BLE001 - 发现永不抛：一个网关的异常不该让面板 500
        logger.warning("模型发现异常 %s", spec.id, exc_info=True)
        result = DiscoveryResult(code="unavailable", fetched_at=now_iso())

    store.put(key, result)
    if result.code in {"ok", "empty"}:
        return result
    # 失败时回旧缓存：列表过期也好过空白，但要如实标 stale
    fallback = store.older(spec.id, key)
    if fallback is not None and fallback.items:
        return replace(fallback, code=result.code, stale=True, http_status=result.http_status)
    return result


async def discover_many(
    specs: list[ProviderSpec], *, force: bool = False, cache: CatalogCache | None = None
) -> dict[str, DiscoveryResult]:
    """并发拉多个网关（信号量限 4）。返回 order 无关的 id → 结果映射。"""
    results = await asyncio.gather(*(discover(spec, force=force, cache=cache) for spec in specs))
    return {spec.id: result for spec, result in zip(specs, results, strict=True)}
