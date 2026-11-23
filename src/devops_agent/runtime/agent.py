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

import os
from collections.abc import Sequence

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.message import Msg, TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase, OpenAIChatModel
from agentscope.tool import Toolkit
from pydantic import SecretStr

from devops_agent.config import RuntimePolicy, load_policy

DEFAULT_SYSTEM_PROMPT = "你是 DevOps 助手，帮助处理持续交付、项目排期与 Bug 定位。"


def build_agent(
    name: str,
    system_prompt: str,
    model: ChatModelBase,
    toolkit: Toolkit | None = None,
    middlewares: Sequence[MiddlewareBase] | None = None,
    max_iters: int | None = None,
    max_retries: int | None = None,
    fallback_model: ChatModelBase | None = None,
    policy: RuntimePolicy | None = None,
) -> Agent:
    """按域装配一个 Agent（交付 / 排期 / 定位共用此工厂）。

    max_iters / max_retries 省略时读 config/policy.yaml；
    max_iters 按域取 react.max_iters.<name>，未命中回退 default。
    """
    resolved = policy if policy is not None else load_policy()
    if fallback_model is None and resolved.model.fallback_model is not None:
        raise ValueError(
            "policy.yaml 已配置 model.fallback_model，请显式传入 fallback_model 模型对象"
        )
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        middlewares=list(middlewares) if middlewares else None,
        react_config=ReActConfig(
            max_iters=max_iters if max_iters is not None else resolved.react.max_iters_for(name)
        ),
        model_config=ModelConfig(
            max_retries=max_retries if max_retries is not None else resolved.model.max_retries,
            fallback_model=fallback_model,
        ),
    )


def build_chat_model() -> OpenAIChatModel:
    """从环境变量构造 OpenAI 兼容模型。

    必填 env（缺失抛 RuntimeError，不打印 key）：
    - DEVOPS_AGENT_API_KEY
    - DEVOPS_AGENT_BASE_URL
    - DEVOPS_AGENT_MODEL
    """
    api_key = os.environ.get("DEVOPS_AGENT_API_KEY")
    base_url = os.environ.get("DEVOPS_AGENT_BASE_URL")
    model_name = os.environ.get("DEVOPS_AGENT_MODEL")
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
    credential = OpenAICredential(api_key=SecretStr(api_key), base_url=base_url)
    # stream=False：网关一次性拿完整回复，简化异常与文本提取路径
    return OpenAIChatModel(credential=credential, model=model_name, stream=False)


def text_from_msg(msg: Msg) -> str:
    """从 Agent 回复消息中提取纯文本（忽略 thinking/tool 块）。"""
    return "".join(block.text for block in msg.get_content_blocks("text"))


class AgentScopeRuntime:
    """真实 Agent 运行时：把异步 Agent.reply 适配到 AgentRuntime 协议。"""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def run(self, session_id: str, user_input: str) -> str:
        msg = Msg(
            name=session_id,
            content=[TextBlock(type="text", text=user_input, id="input", created_at="")],
            role="user",
        )
        reply = await self._agent.reply(msg)
        return text_from_msg(reply)


def build_agentscope_runtime(system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> AgentScopeRuntime:
    """装配真实运行时（env 提供模型凭据）。"""
    agent = build_agent(name="devops", system_prompt=system_prompt, model=build_chat_model())
    return AgentScopeRuntime(agent)
