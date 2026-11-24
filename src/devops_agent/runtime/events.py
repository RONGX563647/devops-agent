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

from devops_agent.runtime.confirm import pending_call

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
# 按事件类型补充的标量字段（仅当值非 None 才输出）。
# hint/source 供 HINT_BLOCK 显示分析提示，block_id 供正文/思考/工具结果块按 id 配对；
# 这些字段此前被投影层丢弃，导致前端拿到的 HINT_BLOCK 是空壳、无法区分同类块。
_SCALAR_FIELDS = (
    "name",
    "role",
    "model_name",
    "tool_call_id",
    "tool_call_name",
    "block_id",
    "delta",
    "state",
    "hint",
    "source",
    "finished_reason",
    "input_tokens",
    "output_tokens",
    "cache_input_tokens",
    "cache_creation_input_tokens",
    # 整块文本：非流式 provider 不发 TEXT_BLOCK_DELTA，只给 TEXT_BLOCK_END，
    # 不放行 text 的话评测拿到的 final_text 恒为空，所有文本断言静默全灭。
    "text",
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
    calls = _duck_tool_calls(event)
    if calls:
        data["tool_calls"] = calls
    return data


def _duck_tool_calls(event: object) -> list[dict[str, Any]]:
    """假事件的 tool_calls：块对象能自己序列化就序列化，否则原样保留 dict。"""
    calls = getattr(event, "tool_calls", None)
    if not isinstance(calls, list):
        return []
    out: list[dict[str, Any]] = []
    for call in calls:
        dumper = getattr(call, "model_dump", None)
        if callable(dumper):
            out.append(dumper(mode="json"))
        elif isinstance(call, dict):
            out.append(call)
    return out


def _tool_call_names(raw: dict[str, Any]) -> str:
    """从事件原生的 tool_calls 列表里取工具名（可能多个），拼成一行。

    REQUIRE_USER_CONFIRM 只带 `tool_calls` 数组，投影层原先丢弃它，
    前端只能显示「等待用户确认」而看不出在等哪个工具。
    """
    calls = raw.get("tool_calls")
    if not isinstance(calls, list):
        return ""
    names = [
        str(call.get("name", "")).strip()
        for call in calls
        if isinstance(call, dict) and call.get("name")
    ]
    return ", ".join(names)


def _confirm_calls(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """把待确认的 tool_calls 摊平成前端能直接渲染的数组。

    只留展示需要的四个字段，且 input 换成「命令本身」而不是原始 JSON：
    前端要用它渲染确认卡片，服务端保留权威 ToolCallBlock 用于真正执行，
    客户端不需要（也不应该）拿到可回传的完整块。
    """
    calls = raw.get("tool_calls")
    if not isinstance(calls, list):
        return []
    result: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        item = pending_call(call)
        result.append(
            {
                "tool_call_id": item.tool_call_id,
                "name": item.name,
                "input": item.input,
                "risk": item.risk,
            }
        )
    return result


def _project(raw: dict[str, Any]) -> dict[str, Any]:
    """把任意来源的字段字典投影成稳定的输出 key 集合。"""
    data: dict[str, Any] = {"type": str(raw.get("type", "UNKNOWN"))}
    for key in ("created_at", "session_id", "reply_id"):
        data[key] = raw.get(key, "") or ""
    for key in _SCALAR_FIELDS:
        value = raw.get(key)
        if value is not None:
            data[key] = value
    if "tool_call_name" not in data:
        names = _tool_call_names(raw)
        if names:
            data["tool_call_name"] = names
    if data["type"] == "REQUIRE_USER_CONFIRM":
        # 这条事件原生的 tool_calls 数组此前被压成一行名字，前端拿不到命令原文，
        # 确认卡片就成了空壳。单独保留一份精简数组。
        calls = _confirm_calls(raw)
        if calls:
            data["tool_calls"] = calls
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
