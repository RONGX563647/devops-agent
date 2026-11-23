"""事件转译层 —— 把 AgentScope 事件对象转成普通 dict。

本模块是 import agentscope 的边界之一（与 agent.py 同属装配区）。
转换后用普通 dict 越过边界，observability 层只认 dict，不依赖框架。

设计要点：
- 优先走事件原生的 `model_dump(mode="json")`：所有 AgentScope 事件都是 Pydantic
  BaseModel，原生就能把 StrEnum / 嵌套 ErrorInfo 转成 JSON 安全值。这是对
  "用内置、不手搓"（AGENTS.md §3.4）的遵守——不再手写 getattr 字段白名单 +
  枚举转换来复刻框架已有的序列化。
- 对 duck-typed 的假事件对象（单测里没有 model_dump 的 dataclass）保留 fallback，
  维持"observability 层可用假事件测试、不绑定框架类型"的韧性。
- 无论走哪条路径，输出都是同一套稳定 key 契约，下游 recorder 与前端按此消费。
"""

from __future__ import annotations

from typing import Any

# 需要落库为 span 的关键事件类型（其余 delta 类事件只用于前端流式渲染）
SPAN_EVENT_TYPES = frozenset(
    {
        "MODEL_CALL_START",
        "MODEL_CALL_END",
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TOOL_RESULT_END",
        "REQUIRE_USER_CONFIRM",
        "EXCEED_MAX_ITERS",
    }
)

# 所有事件都带的基础字段（缺失补空串，保证 key 稳定）
_BASE_FIELDS = ("type", "created_at", "session_id", "reply_id")
# 按事件类型补充的标量字段（仅当值非 None 才输出）
_SCALAR_FIELDS = (
    "name",
    "role",
    "model_name",
    "tool_call_id",
    "tool_call_name",
    "delta",
    "state",
    "finished_reason",
    "input_tokens",
    "output_tokens",
    "cache_input_tokens",
    "cache_creation_input_tokens",
)


def _enum_value(value: Any) -> Any:
    """枚举 → 其 value 字符串；已是原生类型则原样返回（fallback 路径用）。"""
    return getattr(value, "value", value)


def _error_type(error: Any) -> str:
    """错误对象取可序列化的 type 字符串，取不到则退回 str(error)。"""
    if isinstance(error, dict):
        return str(error.get("type", "") or error)
    return str(_enum_value(getattr(error, "type", None)) or error)


def _from_pydantic(event: Any) -> dict[str, Any] | None:
    """真实事件：委托框架原生序列化。无 model_dump 能力返回 None 走 fallback。"""
    dumper = getattr(event, "model_dump", None)
    if not callable(dumper):
        return None
    try:
        raw = dumper(mode="json")
    except (TypeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _from_duck_typed(event: object) -> dict[str, Any]:
    """假事件对象：duck typing 逐字段读取，枚举用 _enum_value 归一。"""
    data: dict[str, Any] = {"type": str(_enum_value(getattr(event, "type", "UNKNOWN")))}
    for key in _SCALAR_FIELDS:
        value = getattr(event, key, None)
        if value is not None:
            data[key] = _enum_value(value)
    for key in ("created_at", "session_id", "reply_id"):
        data[key] = getattr(event, key, "") or ""
    error = getattr(event, "error", None)
    if error is not None:
        data["error"] = _error_type(error)
    return data


def _project(raw: dict[str, Any]) -> dict[str, Any]:
    """把任意来源的字段字典投影成稳定的输出 key 集合。"""
    data: dict[str, Any] = {"type": str(raw.get("type", "UNKNOWN"))}
    for key in ("created_at", "session_id", "reply_id"):
        data[key] = raw.get(key, "") or ""
    for key in _SCALAR_FIELDS:
        value = raw.get(key)
        if value is not None:
            data[key] = value
    error = raw.get("error")
    if error is not None:
        data["error"] = _error_type(error)
    return data


def event_to_dict(event: object) -> dict[str, Any]:
    """把一个事件转成可 JSON 化的普通 dict（稳定 key 契约）。

    真实 AgentScope 事件走原生 model_dump(mode="json")；无此能力的假事件对象
    走 duck typing fallback。两条路径最终都经 _project 归一到同一套输出契约。
    """
    raw = _from_pydantic(event)
    if raw is None:
        raw = _from_duck_typed(event)
    return _project(raw)
