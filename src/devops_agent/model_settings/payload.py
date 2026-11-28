"""响应体与派生值 —— GET / PATCH 共用的一份数据。"""

from __future__ import annotations

from typing import Any

from devops_agent.config import load_model
from devops_agent.model_settings.bounds import (
    EDITABLE,
    READONLY,
    _warnings,
    bounds_payload,
)
from devops_agent.model_settings.file_store import local_path
from devops_agent.model_settings.resolve import Resolved, Source, sources_of, values_of

#: 可以信任的窗口来源：只要命中了其中之一，就不再喊「未校准」。
#: `yaml` / `framework-default` 不在此列 —— 它们只是「有人写了个数字」，
#: 是否可信仍由 `context_calibrated` 表态（SPEC-0018 §6.5 的等价性兜底）。
_CONTEXT_TRUSTED = frozenset(
    {
        Source.STORED.value,
        Source.SESSION.value,
        Source.CALIBRATED.value,
        Source.DISCOVERED.value,
        Source.CARD.value,
    }
)


def _context_uncalibrated(source: str) -> bool:
    """窗口是否可信。旧判据「不是用户填的且 calibrated=false」在此保留原样。

    为什么不写成 `source in CONTEXT_TRUSTED`：匿名 provider 走 `yaml` 静态值，
    若不看 `context_calibrated`，`context_calibrated: true` 的老部署会在升级后
    突然被标成「未校准」——那是 A35 的回归，不是新功能。
    """
    if source in _CONTEXT_TRUSTED:
        return False
    return not load_model().context_calibrated


def derived(values: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    """前端直接用的推算值，不让它自己乘 —— 口径只能有一处。"""
    threshold = int(float(values["trigger_ratio"]) * int(values["context_size"]))
    worst = (1 + int(values["model_max_retries"])) * (1 + int(values["agent_max_retries"]))
    source = str(sources.get("context_size", ""))
    return {
        "compression_threshold": threshold,
        "worst_case_attempts": worst,
        "context_uncalibrated": _context_uncalibrated(source),
        # 让界面能解释「这个窗口是查出来的还是填的」，而不是只给一个二元徽标
        "context_source": source,
        "context_card_may_be_stale": source == Source.CARD.value,
    }


def payload(resolved: dict[str, Resolved], version: int = 0) -> dict[str, Any]:
    """GET / PATCH 共用的响应体。"""
    values = values_of(resolved)
    sources = sources_of(resolved)
    return {
        "effective": values,
        "sources": sources,
        "bounds": bounds_payload(),
        "editable": list(EDITABLE),
        "readonly": dict(READONLY),
        "derived": derived(values, sources),
        "warnings": _warnings(values),
        "version": version,
        # 「部署默认」不再只活在 DB 里：界面要说清写进了哪个文件
        "file_path": str(local_path()),
    }
