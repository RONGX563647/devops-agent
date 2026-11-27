"""配置域注册表 —— 一张表说清「有哪些可配的域、各自写在哪、什么时候生效」。

为什么要注册表而不是每处各写一遍：面板要列全部域、接口要校验可写性、加载器要
知道 yaml 段名，这三件事若各存一份，「新增一个域」就会漏掉其中一处。这里只声明
一次，其余全部由它派生。

`model` 域刻意不由本模块写入：它自己有一套「文件 + 乐观锁」的落盘实现
（`model_settings/`），再塞一条写路径只会制造第二个真相。注册表把它标成
`managed_by`，面板据此显示「去模型抽屉改」。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel
from pydantic.fields import FieldInfo

# 生效时机：改完之后界面据此显示「立即 / 下一轮 / 重启」。不谎报是硬约定。
Hot = Literal["immediate", "next_turn", "restart"]

# 记忆后端的合法取值（与 MemoryPolicy.long_term.backend 的 Literal 同源）
ENV_BACKENDS = ("auto", "mem0", "agentic", "local", "off")


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """一个配置域的元信息。"""

    key: str
    title: str
    section: str
    model: str
    writable: bool = True
    hot: Hot = "immediate"
    note: str = ""
    # 非空表示写入口在别处（如模型抽屉），本接口只读
    managed_by: str = ""


DOMAINS: tuple[DomainSpec, ...] = (
    DomainSpec(
        key="health",
        title="健康分口径",
        section="health",
        model="HealthPolicy",
        note="四类权重、时间与 token 预算、等级阈值；评分时读取，改完下一单即生效。",
    ),
    DomainSpec(
        key="input",
        title="输入管线",
        section="input",
        model="InputPolicy",
        note="抓取超时、体积上限、域名黑白名单；每次抓取读取，改完立即生效。",
    ),
    DomainSpec(
        key="memory",
        title="记忆策略",
        section="memory",
        model="MemoryPolicy",
        hot="next_turn",
        note="三层记忆的裁剪与预算；装配时读取，改完下一轮生效。",
    ),
    DomainSpec(
        key="gateway",
        title="网关策略",
        section="gateway",
        model="GatewayPolicy",
        hot="restart",
        note="确认打扰上限、挂起有效期、SSE 心跳；装配期固定，改完需重启进程。",
    ),
    DomainSpec(
        key="policy",
        title="确认协议（只读）",
        section="policy",
        model="RuntimePolicy",
        writable=False,
        hot="restart",
        note="高危正则与永不沉淀名单是安全域知识：改这里等于给自己发权限，只能走 git 评审。",
    ),
    DomainSpec(
        key="model",
        title="模型参数",
        section="model",
        model="ModelSettings",
        writable=False,
        managed_by="model_settings",
        note="写入口在输入栏齿轮抽屉（会话覆盖 / 全局默认两层）。",
    ),
)

BY_KEY: dict[str, DomainSpec] = {spec.key: spec for spec in DOMAINS}

# 环境变量白名单：这些键由容器注入，优先级高于库里的值，界面上必须锁定。
# 静默地让 UI 覆盖 env，会变成「界面写 5、进程跑 10」这种最难查的偏差。
# 元组第三项是类型，第四项是取值白名单 —— 非法的 env 值沿用旧语义「视为未设置」，
# 而不是让整份配置校验失败（容器里留一个错字不该把服务拦在启动门外）。
ENV_OVERLAY: dict[str, tuple[tuple[str, str, str, tuple[str, ...]], ...]] = {
    "memory": (
        ("enabled", "DEVOPS_AGENT_MEMORY_ENABLED", "bool", ()),
        ("user_id", "DEVOPS_AGENT_MEMORY_USER_ID", "str", ()),
        ("long_term.backend", "DEVOPS_AGENT_MEMORY_BACKEND", "str", ENV_BACKENDS),
        ("long_term.scope", "DEVOPS_AGENT_MEMORY_SCOPE", "str", ("session", "user")),
    ),
}


def model_for(spec: DomainSpec) -> type[BaseModel]:
    """取域的 pydantic 模型。

    延迟 import：`config.py` 会 import 本包的存取层，若这里在模块级 import
    `devops_agent.config` 就会成环。调用发生在请求 / 加载期，届时 config 已装载。
    """
    module = importlib.import_module("devops_agent.config")
    return cast(type[BaseModel], getattr(module, spec.model))


def defaults_of(spec: DomainSpec) -> dict[str, Any]:
    """代码里的默认值（pydantic 字段默认）—— 默认层只有一个出处，不抄进 yaml。"""
    return model_for(spec)().model_dump()


def _constraint(field: FieldInfo, name: str) -> float | int | None:
    """从 Field(ge=/le=/gt=/lt=) 里取边界；没有就返回 None。"""
    for item in field.metadata:
        value = getattr(item, name, None)
        if value is not None:
            return cast(float | int, value)
    return None


def _leaf_bounds(annotation: Any) -> dict[str, Any]:
    """把类型注解读成界面能用的边界：数值区间 + 枚举选项。"""
    bounds: dict[str, Any] = {}
    options = [arg for arg in get_args(annotation) if isinstance(arg, str)]
    if options:
        bounds["options"] = options
    return bounds


def _field_bounds(field: FieldInfo) -> dict[str, Any]:
    bounds = _leaf_bounds(field.annotation)
    for source, target in (
        ("ge", "min"),
        ("gt", "min_exclusive"),
        ("le", "max"),
        ("lt", "max_exclusive"),
    ):
        value = _constraint(field, source)
        if value is not None:
            bounds[target] = value
    return bounds


def bounds_of(spec: DomainSpec) -> dict[str, dict[str, Any]]:
    """展开嵌套模型，给出每个叶子键的边界（前端据此渲染 min/max/选项）。"""
    out: dict[str, dict[str, Any]] = {}
    _walk_bounds(model_for(spec), "", out)
    return out


def _walk_bounds(model: type[BaseModel], prefix: str, out: dict[str, dict[str, Any]]) -> None:
    for name, field in model.model_fields.items():
        key = f"{prefix}{name}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _walk_bounds(annotation, f"{key}.", out)
            continue
        bounds = _field_bounds(field)
        if bounds:
            out[key] = bounds


def env_overlay(domain: str) -> dict[str, Any]:
    """读该域的环境变量覆盖；空串与白名单外的值都视为未设置（旧语义）。"""
    import os

    values: dict[str, Any] = {}
    for key, name, kind, allowed in ENV_OVERLAY.get(domain, ()):
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        if kind == "bool":
            values[key] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif not allowed or raw in allowed:
            values[key] = raw
    return values


__all__ = [
    "BY_KEY",
    "DOMAINS",
    "ENV_OVERLAY",
    "DomainSpec",
    "Hot",
    "bounds_of",
    "defaults_of",
    "env_overlay",
    "model_for",
]
