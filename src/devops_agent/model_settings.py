"""模型参数的解析、校验与派生值 —— 全仓库唯一决定「参数取哪个值」的地方。

优先级（高 → 低）：

    会话覆盖（session_states.model_config）
      > 部署默认（settings['model_config']，前端改、持久化）
      > config/model.yaml
      > 环境变量（仅 model 名）
      > 框架默认（context_size / 重试 / ContextConfig 比例）

**任何地方需要参数，只能走 `resolve()`**，不要自己读 yaml 或 env ——
第二处读取就会出现第二份默认值，而「这个 128000 是谁设的」正是本模块要回答的问题：
每个值都带 `Source`，前端据此画来源徽标。

三个刻意的取舍：

1. `None` 表示「用下层默认」，不是「设成 null 值」。patch 里给 null 等于删除覆盖项，
   否则「把温度改回默认」和「把温度设成 0」在界面上分不开。
2. 推算值（压缩阈值、最坏尝试次数）由后端算好给前端，前端不重复实现口径 ——
   健康分 v1 就是两边各算一套然后对不上。
3. 校验只在这里做。前端可以提前提示，但服务端是唯一裁判。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from devops_agent.config import ModelSettings, load_model
from devops_agent.observability import repository as repo

MODEL_ENV = "DEVOPS_AGENT_MODEL"
# settings 表里这份配置的键名：一张 KV 表装所有「前端可改的配置域」
SETTING_KEY = "model_config"
# 进程内缓存 TTL：配置写进 DB 后可能被另一个进程读到，5 秒足够让单 worker 外的
# 场景在下一轮收敛，又不会让每次读参数都打一次 SQLite。
# 不加 Redis / pub-sub：这个规模不需要第二套通知机制。
STORED_TTL_S = 5.0
# 推理强度：框架 Parameters 允许的取值，超出的会被供应商拒绝
REASONING_EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh")

# 可编辑项：面板里能改的全部字段，顺序即面板顺序。
EDITABLE: tuple[str, ...] = (
    "model",
    "context_size",
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
    "base_url": "环境变量 DEVOPS_AGENT_BASE_URL（重启生效）",
    "stream": "固定 true：关闭会让前端从逐字渲染退化为一次性出现",
    "judge_model": "环境变量 DEVOPS_AGENT_JUDGE_MODEL（重启生效）；属于评测侧配置",
}


class Source(StrEnum):
    """一个参数值是从哪一层来的。前端按它画徽标。"""

    SESSION = "session"
    STORED = "stored"
    YAML = "yaml"
    ENV = "env"
    FRAMEWORK = "framework-default"


@dataclass(frozen=True)
class Resolved:
    """解析结果：值 + 来源。来源不是装饰，它是「缺陷一为什么藏了这么久」的答案。"""

    value: Any
    source: Source


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
    # 模型名不设白名单：当前没有 provider 的模型列表接口。写错要等下一次调用才失败，
    # 面板上因此给了「测试连接」的显式动作。
    "model": Bound("str", nullable=False),
    "context_size": Bound("int", min=8192, max=2_000_000, step=1024),
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


# --- 解析 -------------------------------------------------------------


def _model_name(settings: ModelSettings, declared: set[str]) -> Resolved:
    """模型名：yaml 显式写了就认 yaml，否则环境变量，都没有就空（Echo 场景）。"""
    from_yaml = bool(settings.name) and "name" in declared
    if from_yaml:
        return Resolved(settings.name, Source.YAML)
    env_name = os.environ.get(MODEL_ENV, "")
    if env_name:
        return Resolved(env_name, Source.ENV)
    return Resolved(env_name, Source.FRAMEWORK)


def _scalar(settings: ModelSettings, declared: set[str], key: str, value: Any) -> Resolved:
    """yaml 里显式声明过就是 yaml，否则是框架默认 —— 这是「未校准」的判据。"""
    return Resolved(value, Source.YAML if key in declared else Source.FRAMEWORK)


def _base(domain: str) -> dict[str, Resolved]:
    """yaml + env + 框架默认三层合成的地板值。"""
    settings = load_model()
    declared = set(settings.model_fields_set)
    values: dict[str, Resolved] = {
        "model": _model_name(settings, declared),
        "max_iters": _scalar(settings, declared, "max_iters", settings.max_iters_for(domain)),
    }
    for key in EDITABLE:
        if key in values:
            continue
        values[key] = _scalar(settings, declared, key, getattr(settings, key))
    return values


def _apply_layer(values: dict[str, Resolved], layer: dict[str, Any] | None, source: Source) -> None:
    """把一层覆盖叠上去。null 表示「该项回落下层」，所以跳过而不是写入。"""
    for key, value in (layer or {}).items():
        if key in values and value is not None:
            values[key] = Resolved(value, source)


def resolve(
    session: dict[str, Any] | None = None,
    stored: dict[str, Any] | None = None,
    *,
    domain: str = "devops",
) -> dict[str, Resolved]:
    """逐项解析「值 + 来源」。纯函数：三份 dict 进，一份 dict 出，不碰 DB。"""
    values = _base(domain)
    _apply_layer(values, stored, Source.STORED)
    _apply_layer(values, session, Source.SESSION)
    return values


def values_of(resolved: dict[str, Resolved]) -> dict[str, Any]:
    """去掉来源，只留值。构造模型 / 校验 / 写 run 记录都用这份。"""
    return {key: item.value for key, item in resolved.items()}


# --- 部署默认（DB）的进程内缓存 ----------------------------------------

_stored_cache: tuple[float, dict[str, Any], int] | None = None


def invalidate() -> None:
    """写入部署默认后清缓存：本进程立刻看到自己的改动，别的进程靠 TTL 收敛。"""
    global _stored_cache
    _stored_cache = None


def _read_stored() -> tuple[dict[str, Any], int]:
    """从 settings 表读部署默认；库不可用时返回空配置（回落 yaml/env）。"""
    try:
        with repo.connect() as conn:
            repo.init_db(conn)
            saved = repo.load_setting(conn, SETTING_KEY)
    except Exception:  # noqa: BLE001 - 存储故障不该挡住一次对话
        return {}, 0
    return saved if saved is not None else ({}, 0)


def stored_defaults(*, force: bool = False) -> dict[str, Any]:
    """部署默认参数（前端改的那一层），带 TTL 缓存。"""
    global _stored_cache
    now = time.monotonic()
    if not force and _stored_cache is not None and now - _stored_cache[0] < STORED_TTL_S:
        return _stored_cache[1]
    value, version = _read_stored()
    _stored_cache = (now, value, version)
    return value


def stored_version() -> int:
    """部署默认的版本号，供 GET 与乐观并发使用。"""
    stored_defaults()
    assert _stored_cache is not None  # 上一行保证已填充
    return _stored_cache[2]


def sources_of(resolved: dict[str, Resolved]) -> dict[str, str]:
    """只看来源。前端徽标与 `sources` 响应是同一份数据。"""
    return {key: item.source.value for key, item in resolved.items()}


# --- 校验 -------------------------------------------------------------


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
    return hints


def validate(values: dict[str, Any]) -> list[str]:
    """校验一份完整参数集，返回警告列表；越界抛 `InvalidConfig`。"""
    for field in EDITABLE:
        _check_field(field, values.get(field))
    _check_cross(values)
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


# --- 派生值 -----------------------------------------------------------


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


# --- 下发给模型的那一组值 ---------------------------------------------


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
