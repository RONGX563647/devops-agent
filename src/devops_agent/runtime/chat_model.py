"""模型与上下文配置构造 —— 把环境变量与解析后的参数装配成框架对象。"""

from __future__ import annotations

import os
from typing import Any

from agentscope.agent import ContextConfig
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel
from pydantic import SecretStr

from devops_agent.model_settings import ModelParams, effective_params

# 决定「怎么发一次模型请求」的参数：只有这些变了才重建模型对象。
# 压缩比例、迭代上限只影响调度，热改字段即可。
MODEL_WIRE_KEYS: tuple[str, ...] = (
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

# 联网检索「让谁决定搜不搜」：
# 供应商的 `enable_search` 只是**授权**模型可以搜，搜不搜由模型自由裁量。
# 实测同一份请求体（system prompt + 4 个工具定义 + 只读提示）：
# 带 enable_search 与不带，prompt_tokens 完全一致（3150 vs 3150），
# 即模型一次都没检索；补上 `search_options.forced_search` 后才变成 4436。
# 也就是说只发 enable_search 时，前端的「联网」按钮是个空开关 —— 最糟的中间态。
SEARCH_MODE_ENV = "DEVOPS_AGENT_SEARCH_MODE"

# forced：无条件检索，结果由供应商塞进上下文（默认，符合「点了就该生效」）；
# auto：把裁量权交回模型（省 token，但实测常常不搜）。
_SEARCH_MODE_FORCED = "forced"
_SEARCH_MODE_AUTO = "auto"

_SEARCH_OPTIONS = {"forced_search": True, "search_strategy": "standard"}


def _search_extra_body(enable_search: bool | None) -> dict[str, Any] | None:
    """构造联网检索的请求体附加字段；不联网时返回 None（请求形状保持不变）。

    `enable_search` 显式传入时优先于环境变量：前端逐请求的「联网」开关走这条路。
    """
    if enable_search is None:
        enable_search = os.environ.get("DEVOPS_AGENT_ENABLE_SEARCH") == "1"
    if not enable_search:
        return None
    body: dict[str, Any] = {"enable_search": True}
    # 只有显式写 auto 才放弃强制检索；其余取值（含未设置）一律按 forced，
    # 避免拼错一个环境变量就把「联网」退化成空开关且无人察觉。
    mode = (os.environ.get(SEARCH_MODE_ENV) or _SEARCH_MODE_FORCED).strip().lower()
    if mode != _SEARCH_MODE_AUTO:
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


def _build_model_kwargs(active: ModelParams, enable_search: bool | None) -> dict[str, Any]:
    """读凭据 env 并组装 `OpenAIChatModel` 的构造参数（缺失抛 RuntimeError，不打印 key）。

    `stream=True` 是框架默认值，显式写出以固定契约：模型边生成边返回 chunk，
    AgentScope 才会逐块发出 TEXT_BLOCK_DELTA；关闭后整段文本会在回复结束时
    一次性补发，前端表现为「一次性渲染」（实测 SET 到 DELTA 仅差 0.07ms）。
    联网搜索默认关闭：它带来额外计费与供应商侧的不确定错误，必须显式开启。
    """
    api_key = os.environ.get("DEVOPS_AGENT_API_KEY")
    base_url = os.environ.get("DEVOPS_AGENT_BASE_URL")
    model_name = active.name or None
    missing = [
        n
        for n, v in (
            ("DEVOPS_AGENT_API_KEY", api_key),
            ("DEVOPS_AGENT_BASE_URL", base_url),
            ("DEVOPS_AGENT_MODEL", model_name),
        )
        if not v
    ]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")
    assert api_key is not None and base_url is not None and model_name is not None
    return {
        "credential": OpenAICredential(api_key=SecretStr(api_key), base_url=base_url),
        "model": model_name,
        # None 必须过滤：thinking_enable 等字段是非空 bool，传 None 会被 pydantic 判类型错误
        "parameters": OpenAIChatModel.Parameters(
            **{key: value for key, value in active.sampling().items() if value is not None}
        ),
        "stream": True,
        "max_retries": active.model_max_retries,
        "context_size": active.context_size,
        "extra_body": _search_extra_body(enable_search),
    }


def _build_model(kwargs: dict[str, Any]) -> OpenAIChatModel:
    """用组装好的参数构造模型对象。"""
    return OpenAIChatModel(**kwargs)


def build_chat_model(
    enable_search: bool | None = None,
    params: ModelParams | None = None,
) -> OpenAIChatModel:
    """从环境变量构造 OpenAI 兼容模型。

    必填 env（缺失抛 RuntimeError，不打印 key）：
    - DEVOPS_AGENT_API_KEY
    - DEVOPS_AGENT_BASE_URL
    模型名默认取 env DEVOPS_AGENT_MODEL，`config/model.yaml` 或界面覆盖时以参数为准。

    可选 env：
    - DEVOPS_AGENT_ENABLE_SEARCH：设为 1 时给模型请求体加 `enable_search=true`
      （DashScope 兼容接口的联网搜索开关）；默认关闭。
    - DEVOPS_AGENT_SEARCH_MODE：`forced`（默认）连同 `search_options` 一起下发，
      供应商无条件检索并把结果注入上下文；`auto` 只发 `enable_search`，
      由模型自己决定搜不搜（实测多数情况下不搜）。

    `params` 是解析后的参数集（见 model_settings.ModelParams）：采样参数、
    两级重试、上下文窗口都在里面。**context_size 必须显式传**：换模型不同步窗口，
    会表现为「窗口明明变大，却还是 10 万 token 就压缩」。

    enable_search 显式传入时优先于环境变量：前端逐请求的「联网」开关走这条路。
    """
    # 1) 解析参数：省略时按部署默认
    active = _resolve_wire_params(params)
    # 2) 读 env 凭据并组装构造参数（含联网请求体字段）
    kwargs = _build_model_kwargs(active, enable_search)
    # 3) 构造模型对象
    return _build_model(kwargs)
