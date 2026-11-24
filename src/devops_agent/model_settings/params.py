"""下发给模型的那一组值 —— 采样参数、记录快照与参数指纹。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from devops_agent.config import ModelSettings
from devops_agent.model_settings.resolve import resolve, stored_defaults, values_of


def _get(values: dict[str, Any], key: str, fallback: Any) -> Any:
    """取一项，缺失时用兜底值。单独成函数是为了让 `from_values` 读起来像一张表。"""
    return values.get(key, fallback)


@dataclass(frozen=True)
class ModelParams:
    """一次模型调用真正要用的参数（已解析完，来源信息留在 `Resolved` 里）。"""

    name: str
    context_size: int
    model_max_retries: int
    agent_max_retries: int
    max_iters: int
    temperature: float | None
    max_tokens: int | None
    top_p: float | None
    reasoning_effort: str | None
    thinking_enable: bool | None
    parallel_tool_calls: bool
    trigger_ratio: float
    reserve_ratio: float
    context_buffer_ratio: float

    @classmethod
    def from_values(cls, values: dict[str, Any]) -> ModelParams:
        """从 `values_of(resolve(...))` 构造。缺项用框架默认兜底。"""
        defaults = ModelSettings()
        return cls(
            name=str(_get(values, "model", defaults.name) or ""),
            context_size=int(_get(values, "context_size", defaults.context_size)),
            model_max_retries=int(_get(values, "model_max_retries", defaults.model_max_retries)),
            agent_max_retries=int(_get(values, "agent_max_retries", defaults.agent_max_retries)),
            max_iters=int(_get(values, "max_iters", defaults.max_iters_for("devops"))),
            temperature=_get(values, "temperature", defaults.temperature),
            max_tokens=_get(values, "max_tokens", defaults.max_tokens),
            top_p=_get(values, "top_p", defaults.top_p),
            reasoning_effort=_get(values, "reasoning_effort", defaults.reasoning_effort),
            thinking_enable=_get(values, "thinking_enable", defaults.thinking_enable),
            parallel_tool_calls=bool(
                _get(values, "parallel_tool_calls", defaults.parallel_tool_calls)
            ),
            trigger_ratio=float(_get(values, "trigger_ratio", defaults.trigger_ratio)),
            reserve_ratio=float(_get(values, "reserve_ratio", defaults.reserve_ratio)),
            context_buffer_ratio=float(
                _get(values, "context_buffer_ratio", defaults.context_buffer_ratio)
            ),
        )

    def sampling(self) -> dict[str, Any]:
        """只含采样参数：交给 `OpenAIChatModel.Parameters`。"""
        return {
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "reasoning_effort": self.reasoning_effort,
            "thinking_enable": self.thinking_enable,
            "parallel_tool_calls": self.parallel_tool_calls,
        }

    def record(self) -> dict[str, Any]:
        """写进 `runs.model_params` 的快照：**只记非默认值**。

        全量记录会让每行都塞一份十几键的 JSON，而且默认值一变，
        历史记录看起来就像「参数变了」。
        """
        defaults = ModelSettings()
        out: dict[str, Any] = {}
        if self.name:
            out["model"] = self.name
        pairs = (
            ("context_size", self.context_size, defaults.context_size),
            ("model_max_retries", self.model_max_retries, defaults.model_max_retries),
            ("agent_max_retries", self.agent_max_retries, defaults.agent_max_retries),
            ("max_iters", self.max_iters, defaults.max_iters_for("devops")),
            ("temperature", self.temperature, defaults.temperature),
            ("max_tokens", self.max_tokens, defaults.max_tokens),
            ("top_p", self.top_p, defaults.top_p),
            ("reasoning_effort", self.reasoning_effort, defaults.reasoning_effort),
            ("thinking_enable", self.thinking_enable, defaults.thinking_enable),
            ("parallel_tool_calls", self.parallel_tool_calls, defaults.parallel_tool_calls),
            ("trigger_ratio", self.trigger_ratio, defaults.trigger_ratio),
            ("reserve_ratio", self.reserve_ratio, defaults.reserve_ratio),
            ("context_buffer_ratio", self.context_buffer_ratio, defaults.context_buffer_ratio),
        )
        for key, value, default in pairs:
            if value != default:
                out[key] = value
        return out

    def fingerprint(self) -> str:
        """参数指纹：写进评测批次，参数变了就不可归因。"""
        return params_sha(self.record())


def params_sha(record: dict[str, Any]) -> str:
    """稳定 JSON 的 sha256：键排序，保证同参数同指纹。"""
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def effective_params(
    session: dict[str, Any] | None = None,
    stored: dict[str, Any] | None = None,
    *,
    domain: str = "devops",
) -> ModelParams:
    """一步拿到「该用哪组参数」，供装配与热替换共用。

    `stored` 省略时读部署默认（带 5 秒 TTL）；显式传 `{}` 表示忽略 DB，
    单测与纯函数场景用后者。
    """
    layer = stored_defaults() if stored is None else stored
    return ModelParams.from_values(values_of(resolve(session, layer, domain=domain)))
