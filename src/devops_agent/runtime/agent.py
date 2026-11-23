"""Agent 装配 —— 本模块是唯一直接 import agentscope 的装配点。

agentscope 2.x 真实 API（2.0.12 introspect 已核实）：
- 核心是 Agent + ReActConfig（ReAct 循环由 Agent 实现）
- 其余能力由 Toolkit / Middleware / Workspace / Permission / Pipeline 组合

模型构造示例（需 credential：api_key/base_url）：
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    model = OpenAIChatModel(credential=OpenAICredential(api_key=..., base_url=...), model=...)
"""

from __future__ import annotations

from collections.abc import Sequence

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.tool import Toolkit


def build_agent(
    name: str,
    system_prompt: str,
    model: ChatModelBase,
    toolkit: Toolkit | None = None,
    middlewares: Sequence[MiddlewareBase] | None = None,
    max_iters: int = 12,
    max_retries: int = 3,
) -> Agent:
    """按域装配一个 Agent（交付 / 排期 / 定位共用此工厂）。"""
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        middlewares=list(middlewares) if middlewares else None,
        react_config=ReActConfig(max_iters=max_iters),
        model_config=ModelConfig(max_retries=max_retries),
    )
