"""事件转译层 —— 把 AgentScope 事件对象转成普通 dict。

本模块是 import agentscope 的边界之一（与 agent.py 同属装配区）。
转换后用普通 dict 越过边界，observability 层只认 dict，不依赖框架。

设计要点：
- duck typing 读属性（getattr），不 isinstance 具体事件类，降低对框架内在结构的耦合；
- 只保留可观测的字段，丢弃内部对象引用（避免 dict 里混入不可序列化的东西）；
- 输出结构稳定，前端与 recorder 都按这套 key 消费。
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


def _enum_value(value: Any) -> Any:
    """枚举 → 其 value 字符串；已是原生类型则原样返回。"""
    raw = getattr(value, "value", value)
    return raw


def event_to_dict(event: object) -> dict[str, Any]:
    """把一个 AgentScope 事件对象转成可 JSON 化的普通 dict。

    用 getattr 逐个读字段，缺失时给 None / 空值，保证对事件版本变化有韧性。
    """
    data: dict[str, Any] = {
        "type": str(_enum_value(getattr(event, "type", "UNKNOWN"))),
        "created_at": getattr(event, "created_at", "") or "",
        "session_id": getattr(event, "session_id", "") or "",
        "reply_id": getattr(event, "reply_id", "") or "",
    }
    # 按事件类型补充专属字段（都是标量，可直接序列化）
    for key in (
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
    ):
        value = getattr(event, key, None)
        if value is not None:
            data[key] = _enum_value(value)
    # 错误信息特殊处理：ErrorInfo 对象取可序列化字段
    error = getattr(event, "error", None)
    if error is not None:
        data["error"] = _enum_value(getattr(error, "type", None)) or str(error)
    return data
