"""字段区间、面板字段清单与校验 —— 「哪些字段能改、取值范围是什么」。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 推理强度：框架 Parameters 允许的取值，超出的会被供应商拒绝
REASONING_EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh")

# 可编辑项：面板里能改的全部字段，顺序即面板顺序。
# `provider` 排最前：先选网关才能选模型；`context_auto` 紧跟窗口，因为它是窗口的解释。
EDITABLE: tuple[str, ...] = (
    "provider",
    "model",
    "context_size",
    "context_auto",
    "temperature",
    "max_tokens",
    "top_p",
    "reasoning_effort",
    "thinking_enable",
    "parallel_tool_calls",
    "max_iters",
    "model_max_retries",
    "agent_max_retries",
    "trigger_ratio",
    "reserve_ratio",
    "context_buffer_ratio",
)

# 只读项：出现在面板里但禁用，并说明「在哪改」。
# 完全隐藏会让人以为系统没有这个参数，进而去改代码。
READONLY: dict[str, str] = {
    "api_key": "环境变量 DEVOPS_AGENT_API_KEY（重启生效）；凭据不经 HTTP 往返",
    "base_url": "注册表 config/providers.yaml（多网关），或匿名 provider 的 DEVOPS_AGENT_BASE_URL",
    "providers_file": (
        "config/providers.yaml（设置 › 模型与网关 › 网关 可增删改；保存后热加载，"
        "运行中的会话下次装配生效）"
    ),
    "stream": "固定 true：关闭会让前端从逐字渲染退化为一次性出现",
    "judge_model": "环境变量 DEVOPS_AGENT_JUDGE_MODEL（重启生效）；属于评测侧配置",
}

#: 能力卡 schema 能二次约束的采样字段。调度参数（max_iters/重试/比例）不在其中：
#: 它们与模型能力无关，仍只由 `BOUNDS` 管（SPEC-0018 §6.7）。
SCHEMA_FIELDS: tuple[str, ...] = (
    "max_tokens",
    "temperature",
    "top_p",
    "reasoning_effort",
    "thinking_enable",
    "parallel_tool_calls",
)


class InvalidConfig(ValueError):
    """参数非法。`detail` 直接进 400 响应体，前端照着高亮对应字段。"""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class Bound:
    """一个字段的类型与取值区间；bounds 接口由它序列化，校验由它执行。"""

    kind: str  # int / float / str / bool / enum
    min: float | None = None
    max: float | None = None
    step: float | None = None
    nullable: bool = False
    choices: tuple[str, ...] = ()


BOUNDS: dict[str, Bound] = {
    # 网关 id 不设 enum：BOUNDS 是模块常量，写死 id 列表会在 providers.yaml
    # 加载后失效。合法性由 validate 在运行期用注册表检查（未知 id → 400）。
    "provider": Bound("str", nullable=True),
    # 模型名不设白名单：网关的 /models 常不全，手填是合法用法。写错由
    # `matched_by=none` 的软校验提示，不再等下一次调用才失败。
    "model": Bound("str", nullable=False),
    "context_size": Bound("int", min=8192, max=2_000_000, step=1024),
    # 解析后必是布尔（None 由 resolve 定默认），所以不允许为空
    "context_auto": Bound("bool", nullable=False),
    # 上限 2 是供应商侧的真实边界，不是我们收紧
    "temperature": Bound("float", min=0, max=2, step=0.1, nullable=True),
    "max_tokens": Bound("int", min=1, nullable=True),
    "top_p": Bound("float", min=0, max=1, step=0.05, nullable=True),
    "reasoning_effort": Bound("enum", nullable=True, choices=REASONING_EFFORTS),
    "thinking_enable": Bound("bool", nullable=True),
    "parallel_tool_calls": Bound("bool", nullable=False),
    # 上限给 200 而不是 12：仓库里就有把 max_iters 改成 120 的本地试验，
    # 卡死上限只会逼人去改代码，那比让他在界面上改更糟。
    "max_iters": Bound("int", min=1, max=200, step=1),
    "model_max_retries": Bound("int", min=0, max=5, step=1),
    "agent_max_retries": Bound("int", min=0, max=5, step=1),
    "trigger_ratio": Bound("float", min=0.1, max=0.9, step=0.05),
    "reserve_ratio": Bound("float", min=0.01, max=0.89, step=0.01),
    "context_buffer_ratio": Bound("float", min=0, max=1, step=0.05),
}


def _check_bool(field: str, _bound: Bound, value: Any) -> None:
    if not isinstance(value, bool):
        raise InvalidConfig(field, "应为布尔值")


def _check_enum(field: str, bound: Bound, value: Any) -> None:
    if value not in bound.choices:
        raise InvalidConfig(field, f"可选值：{', '.join(bound.choices)}")


def _check_str(field: str, _bound: Bound, value: Any) -> None:
    # 空串是合法值：它表示「没配模型名」，Echo 自检与测试就是这种状态。
    # 面板据此提示「未配置」，而不是报参数非法。
    if not isinstance(value, str) or len(value) > 128:
        raise InvalidConfig(field, "应为不超过 128 字符的字符串")


def _check_number(field: str, bound: Bound, value: Any) -> None:
    """bool 要单独判：Python 里 isinstance(True, int) 为真，`temperature: true` 会被放过。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidConfig(field, "应为数字")
    if bound.kind == "int" and not isinstance(value, int):
        raise InvalidConfig(field, "应为整数")


_CHECKERS = {
    "bool": _check_bool,
    "enum": _check_enum,
    "str": _check_str,
    "int": _check_number,
    "float": _check_number,
}


def _check_kind(field: str, bound: Bound, value: Any) -> None:
    """按 kind 分派类型检查；未知 kind 按数字处理（BOUNDS 是模块内常量，不会出现）。"""
    _CHECKERS.get(bound.kind, _check_number)(field, bound, value)


def _check_range(field: str, bound: Bound, value: Any) -> None:
    if bound.min is not None and value < bound.min:
        raise InvalidConfig(field, f"不得小于 {bound.min}")
    if bound.max is not None and value > bound.max:
        raise InvalidConfig(field, f"不得大于 {bound.max}")


def _check_field(field: str, value: Any) -> None:
    bound = BOUNDS.get(field)
    if bound is None:
        raise InvalidConfig(field, "未知参数")
    if value is None:
        if bound.nullable:
            return
        raise InvalidConfig(field, "不允许为空")
    _check_kind(field, bound, value)
    if bound.kind in {"int", "float"}:
        _check_range(field, bound, value)


def _check_cross(values: dict[str, Any]) -> None:
    """联动约束：单独看都合法，合起来必错的那种。"""
    if float(values["reserve_ratio"]) >= float(values["trigger_ratio"]):
        raise InvalidConfig("reserve_ratio", "必须小于触发比例，否则压缩后仍超阈值")
    max_tokens = values.get("max_tokens")
    if max_tokens is not None and int(max_tokens) > int(values["context_size"]):
        raise InvalidConfig("max_tokens", "输出上限不能超过上下文窗口，否则必然截断")


def _warnings(values: dict[str, Any]) -> list[str]:
    """只警告不报错：这些是成本提示，不是非法值。"""
    hints: list[str] = []
    worst = (1 + int(values["model_max_retries"])) * (1 + int(values["agent_max_retries"]))
    if worst > 6:
        hints.append(f"最坏情况一次提问会发出 {worst} 次模型请求")
    if int(values["max_iters"]) > 50:
        hints.append(f"单次回复最多 {values['max_iters']} 轮工具调用，耗时与 token 会相应放大")
    effort = values.get("reasoning_effort")
    if values.get("thinking_enable") is False and effort not in (None, "none"):
        hints.append("thinking_enable=false 会覆盖推理强度，后者实际不生效")
    if values.get("context_auto") is False and values.get("provider"):
        # 只在多网关模式下提示：匿名 provider 没有能力卡可查，说了也没法改
        hints.append("窗口按手填值，未跟随模型能力；可开启「按模型重置」让窗口跟随")
    return hints


def _limit(raw: Any) -> float | None:
    """取一个数值型约束；`true` 与 `"5"` 都当「没写」，不拿它们去比较。"""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def _check_schema_value(field: str, constraint: dict[str, Any], value: Any) -> None:
    """按模型卡的 JSON Schema 做二次约束（只收紧，不放宽）。"""
    maximum = _limit(constraint.get("maximum"))
    if maximum is not None and value > maximum:
        raise InvalidConfig(field, f"该模型上限为 {int(maximum)}")
    minimum = _limit(constraint.get("minimum"))
    if minimum is not None and value < minimum:
        raise InvalidConfig(field, f"该模型下限为 {int(minimum)}")
    choices = constraint.get("enum")
    if isinstance(choices, list) and choices and value not in choices:
        raise InvalidConfig(field, f"该模型可选值：{', '.join(str(item) for item in choices)}")


def _check_schema(values: dict[str, Any], schema: dict[str, Any] | None) -> None:
    """能力卡的参数 schema 与 `BOUNDS` 取交集：schema 缺项时按 BOUNDS 放行。

    「schema 没写就任意填」是不允许的 —— 上一行 `_check_field` 已经把 BOUNDS
    的硬底线跑过了，这里只可能更严。
    """
    properties = (schema or {}).get("properties")
    if not isinstance(properties, dict):
        return
    for field in SCHEMA_FIELDS:
        constraint = properties.get(field)
        value = values.get(field)
        if isinstance(constraint, dict) and value is not None:
            _check_schema_value(field, constraint, value)


def validate(values: dict[str, Any], *, schema: dict[str, Any] | None = None) -> list[str]:
    """校验一份完整参数集，返回警告列表；越界抛 `InvalidConfig`。

    `schema` 是当前模型的能力卡参数 schema（可省）：给了就做二次约束，
    不给只走 `BOUNDS` —— 两者是交集，不是替代。
    """
    for field in EDITABLE:
        _check_field(field, values.get(field))
    _check_cross(values)
    _check_schema(values, schema)
    return _warnings(values)


def apply_patch(layer: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把一次 patch 合并进某一层；值为 null 表示删除该项（回落下层）。"""
    merged = {key: value for key, value in layer.items() if key in EDITABLE}
    for key, value in patch.items():
        if key not in EDITABLE:
            raise InvalidConfig(key, "不可编辑或未知参数")
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def bound_payload(field: str) -> dict[str, Any]:
    """单个字段的区间描述：校验失败的 400 里带它，前端据此高亮并给出范围。"""
    bound = BOUNDS.get(field)
    if bound is None:
        return {}
    item: dict[str, Any] = {"kind": bound.kind, "nullable": bound.nullable}
    if bound.min is not None:
        item["min"] = bound.min
    if bound.max is not None:
        item["max"] = bound.max
    if bound.step is not None:
        item["step"] = bound.step
    if bound.choices:
        item["choices"] = list(bound.choices)
    return item


def bounds_payload() -> dict[str, dict[str, Any]]:
    """序列化取值区间，供前端渲染滑杆的上下限。"""
    return {field: bound_payload(field) for field in BOUNDS}
