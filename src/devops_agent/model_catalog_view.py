"""把「当前模型的能力从哪来」翻译成接口字段（SPEC-0022 §7.1）。

从 `model_api` 抽出来的两个理由：
1. `model_api` 已经顶到单文件上限（参数校验 + 读写 + 会话覆盖 + 预览），
   再往里加东西只会让「改一个接口要重读整个文件」重演；
2. 这几个函数是**纯视图**：输入解析后的参数值，输出给前端看的 dict，
   不读库、不写文件、不抛 400 —— 单独放一处，猜它们的副作用成本降到零。

约定的三件事：
- 能力卡查不到不是错误（`matched_by="none"`），面板要能照常渲染；
- `provider` 为空表示「跟随注册表默认」，这里替它算出具体是哪一个，
  否则界面显示空选项、实际却已经连上某个网关；
- 模型名匹配不上只给 warning，不当 400 —— 网关的 `/models` 常不全，
  手填模型名是合法用法。
"""

from __future__ import annotations

import logging
from typing import Any

from devops_agent.model_catalog import ProviderError, get_catalog

logger = logging.getLogger(__name__)


def capability_of(values: dict[str, Any]) -> Any:
    """当前模型的能力卡（四源合并）。查不到返回 None，不抛 —— 面板要能显示。"""
    try:
        return get_catalog().capability(
            str(values.get("provider") or ""), str(values.get("model") or "")
        )
    except Exception:  # noqa: BLE001 - 能力查询失败不该让配置读取 500
        logger.warning("能力卡查询失败", exc_info=True)
        return None


def registry_view(values: dict[str, Any]) -> tuple[int, str]:
    """注册表里有几个网关、当前生效的是哪一个。"""
    provider = str(values.get("provider") or "")
    try:
        registry = get_catalog().registry
    except ProviderError:
        return 0, provider
    return len(registry.providers), provider or registry.default_id


def current_view(cap: Any, model: str) -> dict[str, Any]:
    """当前模型的匹配与来源。查不到能力卡时给一张「什么都没查到」的形状。"""
    if cap is None:
        return {
            "matched_by": "none",
            "context_source": "",
            "card_may_be_stale": False,
            "status": "",
            "deprecated_at": None,
            "label": model,
        }
    context_source = cap.source_of("context_size")
    return {
        "matched_by": cap.matched_by,
        "context_source": context_source,
        "card_may_be_stale": context_source == "card",
        "status": cap.status,
        "deprecated_at": cap.deprecated_at or None,
        "label": cap.label,
    }


def catalog_block(values: dict[str, Any]) -> dict[str, Any]:
    """`/api/model-config` 的 `catalog` 段：这个模型的能力从哪来。"""
    configured, provider = registry_view(values)
    model = str(values.get("model") or "")
    return {
        "providers_configured": configured,
        "provider": provider,
        "current": current_view(capability_of(values), model),
    }


def soft_warnings(values: dict[str, Any]) -> list[str]:
    """模型名匹配不上只警告不报错：手填模型名是合法用法。"""
    cap = capability_of(values)
    if cap is not None and cap.matched_by == "none" and values.get("model"):
        return ["未在内置卡中找到该模型，窗口按未校准处理"]
    return []


__all__ = [
    "capability_of",
    "catalog_block",
    "current_view",
    "registry_view",
    "soft_warnings",
]
