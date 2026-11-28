"""目录门面：把注册表、发现、卡集、合并接成一个对象。

对上层只暴露两件事：`capability()`（同步、只读缓存，给参数解析用）与
`models()/refresh()`（异步、可打上游，给设置面板用）。**对话链路只碰前者**，
所以「网关挂了」永远不会表现成「聊天卡住」。
"""

from __future__ import annotations

import logging
from typing import Any

from devops_agent.model_catalog.cache import CatalogCache, shared
from devops_agent.model_catalog.cards import all_card_names, cards_for, match
from devops_agent.model_catalog.discover import discover
from devops_agent.model_catalog.kinds import kinds_payload
from devops_agent.model_catalog.merge import merge
from devops_agent.model_catalog.registry import Registry, load_registry
from devops_agent.model_catalog.schema import (
    Capability,
    DiscoveryItem,
    DiscoveryResult,
    ProviderError,
    ProviderSpec,
)

logger = logging.getLogger(__name__)


def cache_key(spec: ProviderSpec) -> tuple[str, str, str]:
    """缓存键：换 base_url 或换 key 都必须重取（不同 key 可见模型集可能不同）。"""
    return (spec.id, spec.base_url, spec.key_fingerprint)


class Catalog:
    """一个进程一份。`registry` 延迟读取，注册表文件变化时自动跟上。"""

    def __init__(self, cache: CatalogCache | None = None) -> None:
        self._cache = cache if cache is not None else shared()

    # --- 只读：参数解析路径 ---------------------------------------------

    @property
    def registry(self) -> Registry:
        return load_registry()

    def default_context_auto(self) -> bool:
        """匿名 provider → False（升级不改运行参数）；写了注册表 → True。"""
        registry = self.registry
        return registry.enabled and not registry.anonymous

    def _spec(self, provider_id: str) -> ProviderSpec:
        return self.registry.get(provider_id or None)

    def _manual_item(self, spec: ProviderSpec, model: str) -> DiscoveryItem | None:
        """人工清单里的那一项当作「网关声明」参与合并：它比卡更贴近部署。"""
        for item in spec.models:
            if item.name == model:
                return DiscoveryItem(
                    id=item.name,
                    label=item.label or item.name,
                    context_size=item.context_size,
                    origin="manual",
                )
        return None

    def _cached_item(self, spec: ProviderSpec, model: str) -> DiscoveryItem | None:
        """只看缓存，不发请求 —— 这是「发现不进对话链路」的落实点。"""
        if spec.discover == "manual":
            return self._manual_item(spec, model)
        if spec.discover == "off":
            return None
        result = self._cache.older(spec.id, cache_key(spec))
        if result is None:
            return None
        for item in result.items:
            if item.id == model:
                return item
        return None

    def capability(self, provider_id: str, model: str) -> Capability | None:
        """四源合并。`catalog.enabled=false` 时返回 None（回滚到静态窗口）。"""
        registry = self.registry
        if not registry.enabled or not model:
            return None
        try:
            spec = self._spec(provider_id)
        except ProviderError:
            # 未知 provider 不在这里抛：参数解析路径不该因为一个坏 id 整轮失败
            logger.warning("未知网关 %r，能力合并跳过", provider_id)
            return None
        card, matched_by = match(cards_for(spec.kind), model)
        return merge(
            spec.id,
            model,
            discovered=self._cached_item(spec, model),
            card=card,
            matched_by=matched_by,
        )

    # --- 只读：面板 -----------------------------------------------------

    def providers_payload(self) -> dict[str, Any]:
        """脱敏的网关清单：只回 host、key 指纹，不回完整 URL 与 key。"""
        registry = self.registry
        items: list[dict[str, Any]] = []
        for spec in registry.providers:
            result = self._cache.older(spec.id, cache_key(spec))
            items.append(
                {
                    "id": spec.id,
                    "kind": spec.kind,
                    "label": spec.display,
                    "base_url_host": spec.host,
                    "discover": spec.discover,
                    "has_key": spec.has_key,
                    "key_fingerprint": spec.key_fingerprint,
                    "models": len(result.items) if result else 0,
                    "last_check": (
                        {"at": result.fetched_at, "code": result.code} if result else None
                    ),
                }
            )
        return {
            "default": registry.default_id,
            "anonymous": registry.anonymous,
            "enabled": registry.enabled,
            "items": items,
            "kinds": kinds_payload(),
        }

    def known_models(self, provider_id: str) -> list[str]:
        """该网关「已知」的模型名：缓存 / 人工清单 / 内置卡。

        空列表表示**还没取过**，调用方不能据此判定模型不存在 ——
        写路径据此拒绝会误伤「先配网关、后拉列表」的正常顺序。
        """
        spec = self._spec(provider_id)
        if spec.discover == "off":
            return list(all_card_names(spec.kind))
        if spec.discover == "manual":
            return [item.name for item in spec.models]
        result = self._cache.older(spec.id, cache_key(spec))
        return [item.id for item in result.items] if result else []

    def _items_for(self, spec: ProviderSpec, result: DiscoveryResult | None) -> list[DiscoveryItem]:
        """按 discover 模式给出候选清单（off = 卡里的全部名字）。"""
        if spec.discover == "off":
            return [
                DiscoveryItem(id=name, label=name, origin="card")
                for name in all_card_names(spec.kind)
            ]
        if spec.discover == "manual":
            return [
                DiscoveryItem(
                    id=item.name,
                    label=item.label or item.name,
                    context_size=item.context_size,
                    origin="manual",
                )
                for item in spec.models
            ]
        return list(result.items) if result else []

    def _decorate(self, spec: ProviderSpec, item: DiscoveryItem) -> dict[str, Any]:
        """给一条列表项补上合并后的窗口与状态：面板要能直接显示，不必再逐个请求。"""
        cap = self.capability(spec.id, item.id)
        payload = item.as_dict()
        payload["context_size"] = cap.context_size if cap else item.context_size
        payload["status"] = cap.status if cap else item.status
        payload["matched_by"] = cap.matched_by if cap else "none"
        return payload

    def _matching(self, items: list[DiscoveryItem], query: str) -> list[DiscoveryItem]:
        """按子串过滤：只搜 id 与展示名，不做模糊/拼音匹配（搜不到就是没有）。"""
        if not query:
            return items
        needle = query.lower()
        return [item for item in items if needle in item.id.lower() or needle in item.label.lower()]

    @staticmethod
    def _page(items: list[DiscoveryItem], offset: int, limit: int) -> list[DiscoveryItem]:
        """`limit<=0` 表示「不分页」：面板要全量时用它，而不是猜一个很大的数。"""
        if limit <= 0:
            return items[offset:]
        return items[offset : offset + limit]

    async def models(
        self,
        provider_id: str,
        *,
        query: str = "",
        limit: int = 200,
        offset: int = 0,
        result: DiscoveryResult | None = None,
    ) -> dict[str, Any]:
        """列表端点。`result` 可传入已取到的发现结果，避免重复请求。"""
        spec = self._spec(provider_id)
        if result is None and spec.discover == "models":
            result = await discover(spec)
        items = self._matching(self._items_for(spec, result), query)
        total = len(items)
        page = self._page(items, offset, limit)
        return {
            "provider": spec.id,
            "fetched_at": result.fetched_at if result else "",
            "stale": bool(result.stale) if result else False,
            "code": result.code if result else "ok",
            "http_status": result.http_status if result else None,
            "total": total,
            "items": [self._decorate(spec, item) for item in page],
        }

    async def refresh(self, provider_id: str) -> dict[str, Any]:
        """强制绕过缓存（用户点了刷新）。"""
        spec = self._spec(provider_id)
        if spec.discover != "models":
            # manual/off 没有上游可打，刷新等于重列一遍，不该报错
            return await self.models(provider_id)
        result = await discover(spec, force=True)
        return await self.models(provider_id, result=result)

    def card(self, provider_id: str, model: str) -> dict[str, Any]:
        """单模型能力卡：四源合并 + 逐字段来源。"""
        spec = self._spec(provider_id)
        cap = self.capability(spec.id, model)
        if cap is None:
            # catalog 关闭或空模型名：仍回一张「兜底卡」，让界面有东西可显示
            cap = merge(spec.id, model, calibrated=False)
        return {
            "provider": spec.id,
            "model": model,
            "capability": cap.as_dict(),
            "sources": dict(cap.sources),
            "matched_by": cap.matched_by,
            "matched_card": cap.matched_card,
        }

    def status(self) -> dict[str, Any]:
        """`/health` 用：几个网关、谁缺 key、上次刷新码。"""
        registry = self.registry
        codes: dict[str, str] = {}
        last = ""
        stale = False
        for spec in registry.providers:
            result = self._cache.older(spec.id, cache_key(spec))
            if result is None:
                continue
            codes[spec.id] = result.code
            last = max(last, result.fetched_at)
            stale = stale or result.stale
        return {
            "providers": len(registry.providers),
            "missing_key": registry.missing_key(),
            "last_refresh": last,
            "stale": stale,
            "codes": codes,
            "enabled": registry.enabled,
            "anonymous": registry.anonymous,
        }

    def has_any_key(self) -> bool:
        return self.registry.has_any_key()


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    """进程级单例：参数解析、API、`/health` 共用同一份缓存。"""
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
    return _catalog


def reset(*, cache: CatalogCache | None = None) -> Catalog:
    """测试与装配期重建：换掉单例（可选换一份独立缓存）。"""
    global _catalog
    _catalog = Catalog(cache)
    return _catalog
