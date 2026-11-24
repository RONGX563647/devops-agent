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
from devops_agent.model_settings.resolve import Resolved, Source, sources_of, values_of


def derived(values: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    """前端直接用的推算值，不让它自己乘 —— 口径只能有一处。"""
    threshold = int(float(values["trigger_ratio"]) * int(values["context_size"]))
    worst = (1 + int(values["model_max_retries"])) * (1 + int(values["agent_max_retries"]))
    # 部署者在界面上亲自填过的窗口就算校准过（source 不再是 yaml/framework）
    chosen = sources.get("context_size") in {Source.STORED.value, Source.SESSION.value}
    return {
        "compression_threshold": threshold,
        "worst_case_attempts": worst,
        "context_uncalibrated": (not chosen) and (not load_model().context_calibrated),
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
    }
