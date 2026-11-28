"""多网关模型目录（SPEC-0018）：注册表 → 发现 → 能力合并。

分层（依赖方向单向）：

    registry  ──┐
    discover  ──┼→ catalog ──→ model_settings（通过 CapabilityLookup 端口反向不成立）
    cards     ──┤
    merge     ──┘

`model_settings` 不知道本包存在，只认一个 Protocol；这样参数解析层可以脱离
AgentScope 单测，也把「框架升级」的影响关在本包内。
"""

from __future__ import annotations

from devops_agent.model_catalog.catalog import Catalog, get_catalog, reset
from devops_agent.model_catalog.discover import discover, discover_many
from devops_agent.model_catalog.kinds import (
    KINDS,
    build_client_kwargs,
    build_credential,
    build_parameters,
)
from devops_agent.model_catalog.merge import Calibration, find_calibration, merge
from devops_agent.model_catalog.merge import invalidate as merge_invalidate
from devops_agent.model_catalog.registry import (
    API_KEY_ENV,
    BASE_URL_ENV,
    DEFAULT_ID,
    Registry,
    check_url,
    load_registry,
)
from devops_agent.model_catalog.registry import invalidate as registry_invalidate
from devops_agent.model_catalog.schema import (
    Capability,
    DiscoveryItem,
    DiscoveryResult,
    KindSpec,
    ProviderError,
    ProviderSpec,
)


def reset_catalog() -> Catalog:
    """清掉注册表与校准表缓存并重建单例：换配置目录 / 改 providers.yaml 后调用。

    重建后**必须重新注入**解析层：`model_settings` 手里存的是旧对象，
    只换单例不换注入，等于「换了配置目录但窗口还是旧目录算出来的」。
    测试里每个用例都换 tmp 配置目录，漏了这一步就是整片串味。
    """
    from devops_agent import model_settings

    registry_invalidate()
    merge_invalidate()
    catalog = reset()
    model_settings.set_lookup(catalog)
    return catalog


def configure() -> Catalog:
    """装配期调用一次：建目录单例，并把它注入参数解析层。

    注入而不是层层传参，是为了让 `effective_params()` 的十几个既有调用点
    不用改一行就拿到合并后的窗口 —— 少改一处就少一个「界面 262k、实际 128k」的机会。
    """
    from devops_agent import model_settings

    catalog = get_catalog()
    model_settings.set_lookup(catalog)
    return catalog


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_ID",
    "KINDS",
    "Calibration",
    "Capability",
    "Catalog",
    "DiscoveryItem",
    "DiscoveryResult",
    "KindSpec",
    "ProviderError",
    "ProviderSpec",
    "Registry",
    "build_client_kwargs",
    "build_credential",
    "build_parameters",
    "check_url",
    "configure",
    "discover",
    "discover_many",
    "find_calibration",
    "get_catalog",
    "load_registry",
    "merge",
    "reset_catalog",
]
