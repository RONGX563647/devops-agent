"""跨网关聚合（SPEC-0022 §5.2）：一个网关挂了不该拖垮整张列表。

用假目录而不是真 HTTP：这里验的是「并发取数、逐项降级、全部失败才报错」
这套编排，上游回什么是 `discover` 的事（另有用例）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from devops_agent.model_catalog.aggregate import AggregateUnavailable, aggregate
from devops_agent.model_catalog.schema import ProviderSpec


class FakeRegistry:
    def __init__(self, specs: list[ProviderSpec]) -> None:
        self.providers = tuple(specs)
        self.default_id = specs[0].id if specs else ""

    def ids(self) -> list[str]:
        return [spec.id for spec in self.providers]

    def find(self, provider_id: str | None) -> ProviderSpec | None:
        return next((spec for spec in self.providers if spec.id == provider_id), None)


class FakeCatalog:
    """只实现聚合用到的两个入口；`bodies` 里可以放异常表示该网关超时。"""

    def __init__(self, specs: list[ProviderSpec], bodies: dict[str, Any]) -> None:
        self.registry = FakeRegistry(specs)
        self._bodies = bodies

    async def models(self, provider_id: str, **_kw: Any) -> dict[str, Any]:
        body = self._bodies[provider_id]
        if isinstance(body, BaseException):
            raise body
        return body

    async def refresh(self, provider_id: str) -> dict[str, Any]:
        return await self.models(provider_id)


def _spec(pid: str, label: str = "") -> ProviderSpec:
    return ProviderSpec(
        id=pid,
        kind="openai_chat",
        base_url="https://gw.example.com/v1",
        api_key="none",
        label=label,
    )


def _body(items: list[dict[str, Any]], code: str = "ok") -> dict[str, Any]:
    return {"code": code, "total": len(items), "stale": False, "items": items}


def _row(model: str) -> dict[str, Any]:
    return {
        "id": model,
        "label": model,
        "context_size": 131_072,
        "status": "stable",
        "origin": "upstream",
        "input_types": ["text/plain"],
    }


def test_two_providers_are_grouped_and_decorated() -> None:
    catalog = FakeCatalog(
        [_spec("a", "甲"), _spec("b", "乙")],
        {"a": _body([_row("m1"), _row("m2")]), "b": _body([_row("m3")])},
    )
    out = asyncio.run(aggregate(catalog))  # type: ignore[arg-type]
    assert [item["model"] for item in out["items"]] == ["m1", "m2", "m3"]
    assert {item["provider_label"] for item in out["items"]} == {"甲", "乙"}
    assert [(p["id"], p["code"], p["total"]) for p in out["providers"]] == [
        ("a", "ok", 2),
        ("b", "ok", 1),
    ]
    assert out["truncated"] is False


def test_one_gateway_down_does_not_fail_the_list() -> None:
    catalog = FakeCatalog(
        [_spec("a"), _spec("b")],
        {"a": _body([_row("m1")]), "b": TimeoutError("slow")},
    )
    out = asyncio.run(aggregate(catalog))  # type: ignore[arg-type]
    assert [item["model"] for item in out["items"]] == ["m1"]
    down = next(p for p in out["providers"] if p["id"] == "b")
    assert down["code"] == "unavailable"


def test_all_gateways_down_raises_instead_of_empty_list() -> None:
    """空数组会被读成「没有模型」，与「读不到」必须分开。"""
    catalog = FakeCatalog(
        [_spec("a"), _spec("b")],
        {"a": _body([], "unauthorized"), "b": _body([], "unavailable")},
    )
    with pytest.raises(AggregateUnavailable):
        asyncio.run(aggregate(catalog))  # type: ignore[arg-type]


def test_query_filters_across_providers_and_limit_truncates() -> None:
    catalog = FakeCatalog(
        [_spec("a"), _spec("b")],
        {"a": _body([_row("qwen-max"), _row("glm-4")]), "b": _body([_row("qwen-flash")])},
    )
    found = asyncio.run(aggregate(catalog, query="QWEN"))  # type: ignore[arg-type]
    assert [item["model"] for item in found["items"]] == ["qwen-max", "qwen-flash"]

    capped = asyncio.run(aggregate(catalog, limit=2))  # type: ignore[arg-type]
    assert len(capped["items"]) == 2
    assert capped["truncated"] is True


def test_missing_key_is_reported_per_provider() -> None:
    spec = ProviderSpec(
        id="a", kind="openai_chat", base_url="https://gw.example.com", api_key="env:NOT_SET"
    )
    catalog = FakeCatalog([spec], {"a": _body([])})
    out = asyncio.run(aggregate(catalog, limit=10))  # type: ignore[arg-type]
    assert out["providers"][0]["missing_key"] is True
