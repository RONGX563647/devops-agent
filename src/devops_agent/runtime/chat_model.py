"""模型与上下文配置构造 —— 按注册表取网关，按 kind 取框架类。

三处与旧实现的差别，都是「多网关」逼出来的：

1. 凭据与地址不再写死两个 env，而是来自 `providers.yaml`（匿名 provider 仍读
   那两个 env，所以单网关部署行为不变）。
2. 构造类不再固定 `OpenAIChatModel`：每个 kind 有自己的凭据类与 `Parameters` 类，
   字段集还各不相同，必须按目标类过滤参数。
3. 模型对象按 `(provider, key 指纹, 模型名, wire 指纹, 联网开关)` 缓存：
   少任一项都会复用错对象 —— 把 A 网关的 key 发到 B，或让联网开关静默失效。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from agentscope.agent import ContextConfig

from devops_agent.model_catalog import (
    BASE_URL_ENV,
    DEFAULT_ID,
    KINDS,
    ProviderError,
    ProviderSpec,
    build_client_kwargs,
    build_credential,
    build_parameters,
    get_catalog,
)
from devops_agent.model_catalog.kinds import EXTRA_BODY_KINDS
from devops_agent.model_settings import ModelParams, effective_params

# 常量用 `as` 形式再导出：既有调用点与测试都从本模块取它，挪一次实现不改一圈 import。
from devops_agent.search_mode import SEARCH_MODE_ENV as SEARCH_MODE_ENV
from devops_agent.search_mode import forced_on, search_mode

# 决定「怎么发一次模型请求」的参数：只有这些变了才重建模型对象。
# `provider` 必须在列：换网关意味着换 base_url 与凭据，不重建就会用旧地址发新请求。
MODEL_WIRE_KEYS: tuple[str, ...] = (
    "provider",
    "name",
    "context_size",
    "model_max_retries",
    "temperature",
    "max_tokens",
    "top_p",
    "reasoning_effort",
    "thinking_enable",
    "parallel_tool_calls",
)

# 实例缓存上限与淘汰目标。规模就这么大，不值得写 LRU 计数器。
CACHE_MAX = 32
CACHE_KEEP = 16

# 联网的三种模式（SPEC-0011）：
# - tool（默认）：检索由 `web_search` 工具承担，这里**不发**任何检索字段。
#   否则会变成「供应商强制检索一次 + 模型又调工具搜一次」，重复计费且结果可能冲突。
# - provider：旧行为，把 enable_search / forced_search 写进请求体，服务端搜完注入上下文。
# - off：完全不联网（禁网部署）。
# 模式与「是否强制」的唯一解读点是 `devops_agent.search_mode`，本模块只管发不发。
_SEARCH_OPTIONS = {"forced_search": True, "search_strategy": "standard"}

_model_cache: dict[tuple[Any, ...], Any] = {}
_model_order: list[tuple[Any, ...]] = []


def clear_model_cache() -> None:
    """测试与热替换场景清空实例缓存（换 key / 换注册表后必须重建）。"""
    _model_cache.clear()
    _model_order.clear()


def _search_extra_body(enable_search: bool | None) -> dict[str, Any] | None:
    """构造联网检索的请求体附加字段；不联网时返回 None（请求形状保持不变）。

    `enable_search` 显式传入时优先于环境变量：前端逐请求的「联网」开关走这条路。
    """
    if enable_search is None:
        enable_search = os.environ.get("DEVOPS_AGENT_ENABLE_SEARCH") == "1"
    if not enable_search:
        return None
    # 默认的 tool 模式由工具承担检索，请求体保持原样。
    if search_mode() != "provider":
        return None
    body: dict[str, Any] = {"enable_search": True}
    if forced_on():
        body["search_options"] = dict(_SEARCH_OPTIONS)
    return body


def build_context_config(params: ModelParams) -> ContextConfig:
    """把三个比例装配给框架的 ContextConfig。

    只改这三项，其余（压缩提示词、摘要模板、tool_result_limit）保持框架默认 ——
    这里不是配置的收口点，只是把 yaml/界面上的三个数搬过去。
    """
    return ContextConfig(
        trigger_ratio=params.trigger_ratio,
        reserve_ratio=params.reserve_ratio,
        context_buffer_ratio=params.context_buffer_ratio,
    )


def _resolve_wire_params(params: ModelParams | None) -> ModelParams:
    """省略 params 时按「部署默认」解一遍，保证默认值只有一处。"""
    return params if params is not None else effective_params(stored={})


def wire_sha(active: ModelParams) -> str:
    """只对 `MODEL_WIRE_KEYS` 的当前值做 sha256。

    刻意不用 `params_sha()`：那个基于 `record()`（只记非默认值），两个 provider
    下同参数同模型会算出相同指纹 —— 而这正是要避免的复用。
    """
    payload = {key: getattr(active, key, None) for key in MODEL_WIRE_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def _spec_for(active: ModelParams) -> ProviderSpec:
    """解析当前生效的网关；未知 id 直接报错（不静默回落到默认网关）。"""
    return get_catalog().registry.get(active.provider or None)


def _missing(active: ModelParams, spec: ProviderSpec) -> list[str]:
    """缺什么就报什么。匿名 provider 沿用旧变量名，老部署的运维习惯不变。"""
    missing: list[str] = []
    if not spec.base_url:
        missing.append(BASE_URL_ENV if spec.id == DEFAULT_ID else f"providers.{spec.id}.base_url")
    if not spec.has_key:
        missing.append(spec.key_env or f"providers.{spec.id}.api_key")
    if not active.name:
        missing.append(f"DEVOPS_AGENT_MODEL（或 providers.{spec.id}.models 里的模型名）")
    return missing


def _check_ready(active: ModelParams, spec: ProviderSpec) -> None:
    missing = _missing(active, spec)
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")
    if spec.kind not in KINDS:
        raise ProviderError(f"providers.{spec.id}.kind", f"未知协议 {spec.kind!r}")


def _kwargs(active: ModelParams, spec: ProviderSpec, enable_search: bool | None) -> dict[str, Any]:
    """组装构造参数。只放目标类真正接受的键：多一个就是 TypeError。"""
    kwargs: dict[str, Any] = {
        "credential": build_credential(spec),
        "model": active.name,
        # None 要过滤：thinking_enable 等是非空 bool，传 None 会被 pydantic 判类型错误
        "parameters": build_parameters(spec.kind, active.sampling()),
        "stream": True,
        "max_retries": active.model_max_retries,
        "context_size": active.context_size,
    }
    # 联网字段只给接受 `extra_body` 的类；发给不接受的类会被供应商 400
    if spec.kind in EXTRA_BODY_KINDS:
        extra = _search_extra_body(enable_search)
        if extra:
            kwargs["extra_body"] = extra
    client_kwargs = build_client_kwargs(spec)
    if client_kwargs:
        kwargs["client_kwargs"] = client_kwargs
    return kwargs


def _search_signature(enable_search: bool | None) -> tuple[bool | None, bool, str, bool]:
    """决定请求体里检索字段的全部输入：开关、部署默认、模式、是否强制。

    这些必须进缓存键。少任一项都会出现「切了联网开关，模型仍按旧请求体发」——
    而那正是 SPEC-0011 修复过的自相矛盾状态。
    """
    return (
        enable_search,
        os.environ.get("DEVOPS_AGENT_ENABLE_SEARCH") == "1",
        search_mode(),
        forced_on(),
    )


def _cache_key(
    spec: ProviderSpec, active: ModelParams, enable_search: bool | None
) -> tuple[Any, ...]:
    """实例缓存键：网关、key 指纹、模型、wire 指纹、检索签名。

    `key_fingerprint` 必须在列：同一 provider 换了 env 里的 key 就该重建，
    否则会把旧 key 继续发出去（测试里每个用例都在 patch env，这条漏了就是整片失败）。
    """
    return (
        spec.id,
        spec.key_fingerprint,
        active.name,
        wire_sha(active),
        *_search_signature(enable_search),
    )


def _remember(key: tuple[Any, ...], model: Any) -> None:
    """写入缓存并按「最老优先」淘汰；不做 LRU 计数，规模不需要。"""
    _model_cache[key] = model
    _model_order.append(key)
    if len(_model_order) > CACHE_MAX:
        drop = len(_model_order) - CACHE_KEEP
        for old in _model_order[:drop]:
            _model_cache.pop(old, None)
        del _model_order[:drop]


def build_chat_model(
    enable_search: bool | None = None,
    params: ModelParams | None = None,
) -> Any:
    """按注册表构造模型对象（带 wire 指纹缓存）。

    必填项缺失抛 `RuntimeError`（不打印 key）：匿名 provider 下仍是最多三个环境变量，
    写了 `providers.yaml` 时期望的是 `env:NAME` 指向的变量。

    `params` 是解析后的参数集（见 model_settings.ModelParams）：采样参数、
    两级重试、上下文窗口都在里面。**context_size 必须显式传**：换模型不同步窗口，
    会表现为「窗口明明变大，却还是 10 万 token 就压缩」。

    `enable_search` 显式传入时优先于环境变量：前端逐请求的「联网」开关走这条路。
    它必须进缓存键 —— 否则切开关时会命中旧对象，请求体里的检索字段不跟着变。
    """
    active = _resolve_wire_params(params)
    spec = _spec_for(active)
    _check_ready(active, spec)

    key = _cache_key(spec, active, enable_search)
    cached = _model_cache.get(key)
    if cached is not None:
        return cached
    model = KINDS[spec.kind].model_cls(**_kwargs(active, spec, enable_search))
    _remember(key, model)
    return model
