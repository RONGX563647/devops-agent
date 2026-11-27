"""解析层看到的能力查询端口 —— 结构化 Protocol，不 import `model_catalog`。

为什么必须是 Protocol 而不是直接 import：`model_settings` 现在**不依赖 AgentScope**
（纯逻辑，单测不需要装模型、也不受框架升级影响）。`model_catalog` 依赖框架，
一旦反向 import，这层就被拉进框架的变更半径里。依赖方向只能是
`model_catalog → model_settings`。

`CapabilityView` 只描述解析层真正要用的两件事：窗口值、以及这个值的来源。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CapabilityView(Protocol):
    """四源合并后的能力卡（与 `model_catalog.schema.Capability` 结构相容）。"""

    @property
    def context_size(self) -> int: ...

    def source_of(self, key: str) -> str: ...


@runtime_checkable
class CapabilityLookup(Protocol):
    """能力查询器。`capability()` 拿不到就是 None（未匹配/未配置），不抛异常。"""

    def capability(self, provider_id: str, model: str) -> CapabilityView | None: ...

    def default_context_auto(self) -> bool:
        """`context_auto` 未显式设置时的默认：匿名 provider → False，有注册表 → True。"""
        ...


# --- 进程级注入点 -------------------------------------------------------
#
# 为什么是模块级而不是层层传参：`effective_params()` 有十几个调用点
# （装配、热替换、API、健康检查），每个都记得传一个 lookup 是不可能的，
# 忘一个就会出现「界面显示 262k、实际按 128k 压缩」—— 正是本 spec 要消灭的
# 两套口径。装配期注入一次，其余照旧。
#
# 默认 None：单测与纯函数场景不装任何东西，行为与改动前逐字段相同（A35）。

_lookup: CapabilityLookup | None = None


def set_lookup(lookup: CapabilityLookup | None) -> None:
    """装配期注入（或测试里替换）。传 None 等于关闭能力合并。"""
    global _lookup
    _lookup = lookup


def get_lookup() -> CapabilityLookup | None:
    return _lookup
