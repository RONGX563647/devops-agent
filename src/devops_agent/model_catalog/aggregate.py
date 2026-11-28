"""跨网关模型聚合（SPEC-0022 §5.2）：会话里只选模型，不选网关。

三条刻意的取舍：

1. **逐网关并发 + 单项 3s 超时**：一个网关挂了不该让整张列表转圈。
2. **全部失败才 503**：空数组会被读成「没有模型」，与「读不到」必须分开 ——
   沿用 `/api/providers` 的 `providers_invalid` 立场。
3. **缓存优先**：只有 `refresh=true` 才绕过缓存；打开选择器不等于打一遍上游。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from devops_agent.model_catalog.catalog import Catalog
from devops_agent.model_catalog.schema import ProviderError, ProviderSpec

#: 单个网关的等待上限：界面最多等 3 秒，超时按不可用记。
TIMEOUT_SECONDS = 3.0

DEFAULT_LIMIT = 300
MAX_LIMIT = 500


class AggregateUnavailable(RuntimeError):
    """所有网关都没取到列表：调用方回 503，而不是一个空数组。"""


def _capabilities(row: dict[str, Any]) -> list[str]:
    """把网关声明的模态翻译成能力标签（前端只用来做小徽标）。"""
    caps = ["chat"]
    inputs = row.get("input_types") or []
    if any("image" in str(item) for item in inputs):
        caps.append("vision")
    return caps


def _item(spec: ProviderSpec, row: dict[str, Any]) -> dict[str, Any]:
    origin = str(row.get("origin") or "")
    return {
        "provider": spec.id,
        "provider_label": spec.display,
        "model": str(row.get("id") or ""),
        "label": str(row.get("label") or row.get("id") or ""),
        "context": row.get("context_size"),
        "status": str(row.get("status") or ""),
        "capabilities": _capabilities(row),
        "source": "discovered" if origin == "upstream" else origin or "discovered",
    }


def _provider_row(
    spec: ProviderSpec, body: dict[str, Any] | None, error: str = ""
) -> dict[str, Any]:
    body = body or {}
    return {
        "id": spec.id,
        "label": spec.display,
        "code": error or str(body.get("code") or "unavailable"),
        "total": int(body.get("total") or 0),
        "stale": bool(body.get("stale")),
        "missing_key": not spec.has_key,
    }


async def _one(catalog: Catalog, provider_id: str, refresh: bool) -> dict[str, Any]:
    """取一个网关的全部候选。`limit=0` 表示不分页 —— 聚合后再统一截。"""
    async with asyncio.timeout(TIMEOUT_SECONDS):
        if refresh:
            return await catalog.refresh(provider_id)
        return await catalog.models(provider_id, limit=0)


def _failed(row: dict[str, Any]) -> bool:
    return bool(row["code"] != "ok" and row["total"] == 0)


def _wanted(catalog: Catalog, provider: str) -> list[ProviderSpec]:
    """要查的网关：不指定就是全部；指定了不存在的 id 直接 400，不静默回空。"""
    registry = catalog.registry
    specs = [spec for spec in registry.providers if not provider or spec.id == provider]
    if provider and not specs:
        raise ProviderError("provider", f"未知网关 {provider!r}；可用：{', '.join(registry.ids())}")
    return specs


def _merge(
    specs: list[ProviderSpec], results: list[Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把并发结果摊成「模型清单 + 每网关状态」。单项异常只记状态。"""
    items: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    for spec, result in zip(specs, results, strict=False):
        if isinstance(result, BaseException):
            providers.append(_provider_row(spec, None, "unavailable"))
            continue
        providers.append(_provider_row(spec, result))
        items.extend(_item(spec, row) for row in result.get("items") or [])
    return items, providers


def _matched(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return items
    return [
        item for item in items if needle in item["model"].lower() or needle in item["label"].lower()
    ]


async def aggregate(
    catalog: Catalog,
    *,
    query: str = "",
    provider: str = "",
    limit: int = DEFAULT_LIMIT,
    refresh: bool = False,
) -> dict[str, Any]:
    """跨网关模型列表 + 每个网关的状态 + 当前默认选择。"""
    specs = _wanted(catalog, provider)
    started = time.perf_counter()
    results = await asyncio.gather(
        *(_one(catalog, spec.id, refresh) for spec in specs),
        return_exceptions=True,
    )
    items, providers = _merge(specs, list(results))

    if providers and all(_failed(row) for row in providers):
        raise AggregateUnavailable(
            "；".join(f"{row['id']}={row['code']}" for row in providers) or "无可用网关"
        )

    items = _matched(items, query)
    cap = max(1, min(MAX_LIMIT, limit))
    return {
        "items": items[:cap],
        "providers": providers,
        "default": {"provider": catalog.registry.default_id, "model": ""},
        "truncated": len(items) > cap,
        # 只用于日志/观测：前端不依赖它
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


__all__ = [
    "AggregateUnavailable",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "TIMEOUT_SECONDS",
    "aggregate",
]
