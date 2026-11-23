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
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.message import Msg, TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase, OpenAIChatModel
from agentscope.permission import PermissionContext
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from pydantic import SecretStr

from devops_agent.config import RuntimePolicy, load_policy
from devops_agent.governance.policy import build_permission_context
from devops_agent.runtime.events import event_to_dict
from devops_agent.tools.catalog import build_readonly_toolkit, shell_name

DEFAULT_SYSTEM_PROMPT = "你是 DevOps 助手，帮助处理持续交付、项目排期与 Bug 定位。"

# 装配用的工作目录：部署环境可用 DEVOPS_AGENT_WORKDIR 指定 Agent 能看的工作区，
# 缺省取进程当前目录（本地开发即仓库根）。工具的相对路径与 shell 的 cwd 都基于它。
WORKDIR_ENV = "DEVOPS_AGENT_WORKDIR"

# 工具约定必须写进系统提示：修复前 Agent 收到的只有一句角色描述，
# 模型不知道自己有工具，于是把「工具调用 functions.exec」写进正文后直接结束。
_TOOL_PROMPT = """你可以调用以下工具来获取真实信息，而不是凭记忆回答：
- shell（工具名 {shell_tool}）：执行命令行，语法为 {shell}，工作目录 {workdir}
- {read_tool} / {glob_tool} / {grep_tool}：读文件、按模式找文件、按正则搜内容

硬性要求：
1. 需要文件内容、目录列表、命令输出时，必须真的发起工具调用；
   不要用「工具调用 xxx」这样的文字描述代替调用。
2. 不要编造命令输出或文件内容。工具失败就据实说明失败原因，
   并改用其他只读手段（如 {glob_tool} / {grep_tool}）重试。
3. 只读会话没有写权限：不要尝试 Edit/Write 类操作，被拒绝时如实说明。
4. 最终回答用中文，可用 Markdown（标题、列表、表格、代码块）；引用工具输出时保持原样。"""


def resolve_workdir(workdir: str | os.PathLike[str] | None = None) -> Path:
    """确定 Agent 的工作目录：显式参数 > 环境变量 > 进程当前目录。"""
    if workdir is not None:
        return Path(workdir).expanduser().resolve()
    return Path(os.environ.get(WORKDIR_ENV) or os.getcwd()).expanduser().resolve()


def build_system_prompt(workdir: str | os.PathLike[str] | None = None) -> str:
    """拼装系统提示：角色描述 + 可用工具契约 + 运行环境。"""
    base = resolve_workdir(workdir)
    return "\n\n".join(
        [
            DEFAULT_SYSTEM_PROMPT,
            _TOOL_PROMPT.format(
                shell_tool="Bash",
                shell=shell_name(),
                workdir=base,
                read_tool="Read",
                glob_tool="Glob",
                grep_tool="Grep",
            ),
        ]
    )


def build_agent(
    name: str,
    system_prompt: str,
    model: ChatModelBase,
    toolkit: Toolkit | None = None,
    middlewares: Sequence[MiddlewareBase] | None = None,
    max_iters: int | None = None,
    max_retries: int | None = None,
    policy: RuntimePolicy | None = None,
    state: AgentState | None = None,
) -> Agent:
    """按域装配一个 Agent（交付 / 排期 / 定位共用此工厂）。

    max_iters / max_retries 省略时读 config/policy.yaml；
    max_iters 按域取 react.max_iters.<name>，未命中回退 default。
    state 用于注入权限上下文等跨轮状态（省略则 Agent 自建默认状态）。
    """
    resolved = policy if policy is not None else load_policy()
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        middlewares=list(middlewares) if middlewares else None,
        state=state,
        react_config=ReActConfig(
            max_iters=max_iters if max_iters is not None else resolved.react.max_iters_for(name)
        ),
        model_config=ModelConfig(
            max_retries=max_retries if max_retries is not None else resolved.model.max_retries,
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
    # stream=True（框架默认值，显式写出以固定契约）：模型边生成边返回 chunk，
    # AgentScope 才会逐块发出 TEXT_BLOCK_DELTA。若关闭，整段文本会作为单个 delta
    # 在回复结束时瞬间补发 —— 前端表现为「一次性渲染」，实测 SET 到 DELTA 仅差 0.07ms。
    return OpenAIChatModel(credential=credential, model=model_name, stream=True)


def text_from_msg(msg: Msg) -> str:
    """从 Agent 回复消息中提取纯文本（忽略 thinking/tool 块）。"""
    return "".join(block.text for block in msg.get_content_blocks("text"))


class AgentScopeRuntime:
    """真实 Agent 运行时：把异步 Agent.reply 适配到 AgentRuntime 协议。"""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    @staticmethod
    def _make_msg(session_id: str, user_input: str) -> Msg:
        return Msg(
            name=session_id,
            content=[TextBlock(type="text", text=user_input, id="input", created_at="")],
            role="user",
        )

    async def run(self, session_id: str, user_input: str) -> str:
        reply = await self._agent.reply(self._make_msg(session_id, user_input))
        return text_from_msg(reply)

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """消费 AgentScope reply_stream，逐个转成框架无关的 dict 往外吐。

        事件对象只在本方法内存在，越过边界的只有普通 dict：
        这是"agent.py 单一装配点"约束在流式路径上的延续。
        """
        async for event in self._agent.reply_stream(self._make_msg(session_id, user_input)):
            yield event_to_dict(event)


def build_agentscope_runtime(
    system_prompt: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    permission_context: PermissionContext | None = None,
) -> AgentScopeRuntime:
    """装配真实运行时（env 提供模型凭据）。

    - 工具：内置只读工具集（shell/read/glob/grep），不装配写工具；
    - 权限：注入 policy.yaml 的权限上下文。模式缺省为 `dont_ask` ——
      服务端没有人工确认通道，ASK 只会发出一个确认事件然后结束这一轮。
    """
    base = resolve_workdir(workdir)
    agent = build_agent(
        name="devops",
        system_prompt=system_prompt if system_prompt is not None else build_system_prompt(base),
        model=build_chat_model(),
        toolkit=build_readonly_toolkit(base),
        state=AgentState(
            permission_context=(
                permission_context if permission_context is not None else build_permission_context()
            )
        ),
    )
    return AgentScopeRuntime(agent)
