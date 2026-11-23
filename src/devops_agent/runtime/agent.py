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

import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.event import (
    ConfirmResult,
    ExternalExecutionResultEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)
from agentscope.message import (
    Msg,
    TextBlock,
    ToolCallBlock,
    ToolCallState,
    ToolResultBlock,
    ToolResultState,
)
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase, OpenAIChatModel
from agentscope.permission import PermissionBehavior, PermissionRule
from agentscope.state import AgentState, ToolContext
from agentscope.tool import AskUserAnswer, AskUserMetadata, AskUserParams, Toolkit
from pydantic import SecretStr

from devops_agent.config import RuntimePolicy, load_policy
from devops_agent.governance.policy import build_permission_context
from devops_agent.governance.profiles import PermissionProfile, default_profile
from devops_agent.runtime.confirm import (
    ConfirmDecision,
    ExternalAnswer,
    effective_scope,
    pending_call,
    rule_content_for,
    validate_answers,
)
from devops_agent.runtime.events import event_to_dict
from devops_agent.runtime.profile_note import ProfileNoteMiddleware
from devops_agent.secrets.middleware import build_secret_middleware
from devops_agent.tools.catalog import (
    activated_groups,
    build_full_toolkit,
    shell_name,
)

DEFAULT_SYSTEM_PROMPT = "你是 DevOps 助手，帮助处理持续交付、项目排期与 Bug 定位。"

# 装配用的工作目录：部署环境可用 DEVOPS_AGENT_WORKDIR 指定 Agent 能看的工作区，
# 缺省取进程当前目录（本地开发即仓库根）。工具的相对路径与 shell 的 cwd 都基于它。
WORKDIR_ENV = "DEVOPS_AGENT_WORKDIR"

# 工具约定必须写进系统提示：修复前 Agent 收到的只有一句角色描述，
# 模型不知道自己有工具，于是把「工具调用 functions.exec」写进正文后直接结束。
#
# 注意：这一段**不含任何随会话变化的内容**。前缀缓存按字节比对，这里改一个字，
# 后面的工具 schema 与整段历史一起作废（实测跨档命中 21%）。档位说明因此不在
# 系统提示里，而是由 ProfileNoteMiddleware 追加在每次请求的最后一条消息。
_TOOL_PROMPT = """你可以调用以下工具来获取真实信息，而不是凭记忆回答：
- shell（工具名 {shell_tool}）：执行命令行，语法为 {shell}，工作目录 {workdir}
- {read_tool} / {glob_tool} / {grep_tool}：读文件、按模式找文件、按正则搜内容
硬性要求：
1. 需要文件内容、目录列表、命令输出时，必须真的发起工具调用；
   不要用「工具调用 xxx」这样的文字描述代替调用。
2. 不要编造命令输出或文件内容。工具失败就据实说明失败原因，
   并改用其他只读手段（如 {glob_tool} / {grep_tool}）重试。
3. 最终回答用中文，可用 Markdown（标题、列表、表格、代码块）；引用工具输出时保持原样。"""


def resolve_workdir(workdir: str | os.PathLike[str] | None = None) -> Path:
    """确定 Agent 的工作目录：显式参数 > 环境变量 > 进程当前目录。"""
    if workdir is not None:
        return Path(workdir).expanduser().resolve()
    return Path(os.environ.get(WORKDIR_ENV) or os.getcwd()).expanduser().resolve()


def build_system_prompt(workdir: str | os.PathLike[str] | None = None) -> str:
    """拼装系统提示：角色描述 + 可用工具契约。

    **整段与会话无关、逐字节稳定**——这是前缀缓存真正能复用的部分。
    随档位变化的内容（能不能写、越界怎么办）不放在这里：系统提示在请求最前面，
    那里改一个字，后面的工具 schema 与整段历史全部作废（实测跨档命中 21%）。
    档位约束改由 `runtime/profile_note.py` 追加在消息末尾（实测 96%）。
    """
    base = resolve_workdir(workdir)
    tool_prompt = _TOOL_PROMPT.format(
        shell_tool="Bash",
        shell=shell_name(),
        workdir=base,
        read_tool="Read",
        glob_tool="Glob",
        grep_tool="Grep",
    )
    return "\n\n".join([DEFAULT_SYSTEM_PROMPT, tool_prompt])


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


def build_chat_model(enable_search: bool | None = None) -> OpenAIChatModel:
    """从环境变量构造 OpenAI 兼容模型。

    必填 env（缺失抛 RuntimeError，不打印 key）：
    - DEVOPS_AGENT_API_KEY
    - DEVOPS_AGENT_BASE_URL
    - DEVOPS_AGENT_MODEL

    可选 env：
    - DEVOPS_AGENT_ENABLE_SEARCH：设为 1 时给模型请求体加 `enable_search=true`
      （DashScope 兼容接口的联网搜索开关）；默认关闭。

    enable_search 显式传入时优先于环境变量：前端逐请求的「联网」开关走这条路。
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
    # 联网搜索默认关闭：它会带来额外计费与供应商侧的不确定错误，必须显式开启。
    if enable_search is None:
        enable_search = os.environ.get("DEVOPS_AGENT_ENABLE_SEARCH") == "1"
    return OpenAIChatModel(
        credential=credential,
        model=model_name,
        stream=True,
        extra_body={"enable_search": True} if enable_search else None,
    )


def _state_for(profile: PermissionProfile, state: AgentState | None) -> AgentState:
    """准备跨轮状态：没有就新建；权限上下文不一致就重建。

    比较整份 PermissionContext 而不是只比 mode：workspace 与 execute 的
    mode 都是 accept_edits，只比 mode 会让 execute 的命令白名单丢失。

    档位切换必须丢弃旧的 allow_rules —— 那些规则是在另一个信任级别下点
    「本会话允许」攒的，跨档继续生效等于绕过用户这次的选择。
    """
    desired = build_permission_context(profile=profile)
    if state is None:
        return AgentState(
            permission_context=desired,
            tool_context=ToolContext(activated_groups=activated_groups()),
        )
    if state.permission_context.model_dump() != desired.model_dump():
        state.permission_context = desired
    return state


class AgentScopeRuntime:
    """真实 Agent 运行时：把 Agent.reply_stream 适配到 AgentRuntime 协议。"""

    def __init__(self, agent: Agent, *, profile: PermissionProfile | None = None) -> None:
        self._agent = agent
        self._profile = profile if profile is not None else default_profile()

    @property
    def profile(self) -> PermissionProfile:
        return self._profile

    @staticmethod
    def _make_msg(session_id: str, user_input: str) -> Msg:
        return Msg(
            name=session_id,
            content=[TextBlock(type="text", text=user_input, id="input", created_at="")],
            role="user",
        )

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """消费 AgentScope reply_stream，逐个转成框架无关的 dict 往外吐。

        事件对象只在本方法内存在，越过边界的只有普通 dict：
        这是"agent.py 单一装配点"约束在流式路径上的延续。
        """
        async for event in self._agent.reply_stream(self._make_msg(session_id, user_input)):
            yield event_to_dict(event)

    # --- 挂起与恢复 ---------------------------------------------------
    def snapshot(self) -> str:
        """把 AgentState 序列化成 JSON：会话隔离、刷新恢复、跨实例续跑都靠它。"""
        return json.dumps(
            self._agent.state.model_dump(mode="json"), ensure_ascii=False, default=str
        )

    def awaiting_blocks(self) -> list[ToolCallBlock]:
        """当前仍等待外部响应的 tool_call（含 ASK 与 external 两种）。"""
        return list(self._agent.state.get_awaiting_tool_calls(self._agent.name))

    def _blocks_in_state(self, state: ToolCallState) -> list[ToolCallBlock]:
        return [block for block in self.awaiting_blocks() if block.state == state]

    def confirm_blocks(self) -> list[ToolCallBlock]:
        """等待用户确认的调用（ASKING）。"""
        return self._blocks_in_state(ToolCallState.ASKING)

    def external_blocks(self) -> list[ToolCallBlock]:
        """等待外部执行结果的调用（SUBMITTED，目前只有 AskUser）。"""
        return self._blocks_in_state(ToolCallState.SUBMITTED)

    def pending_kind(self) -> str | None:
        """挂起类型：question（AskUser）/ confirm（权限）/ None。"""
        if self.external_blocks():
            return "question"
        if self.confirm_blocks():
            return "confirm"
        return None

    def pending_calls(self) -> list[dict[str, Any]]:
        """给前端展示的精简确认项（不含可回传的完整块）。"""
        return [
            asdict(pending_call(block.model_dump(mode="json"))) for block in self.confirm_blocks()
        ]

    def pending_questions(self) -> list[dict[str, Any]]:
        """把 AskUser 的入参解析成普通 dict，越过框架边界。"""
        items: list[dict[str, Any]] = []
        for block in self.external_blocks():
            params = AskUserParams.model_validate_json(block.input)
            items.append(
                {
                    "tool_call_id": block.id,
                    "name": block.name,
                    "questions": [q.model_dump(mode="json") for q in params.questions],
                }
            )
        return items

    @staticmethod
    def _rules_for(decision: ConfirmDecision, block: ToolCallBlock) -> list[PermissionRule] | None:
        """按作用域生成要沉淀的规则；高危命令强制降级为「只这一次」。"""
        if not decision.confirmed:
            return None
        scope = effective_scope(block.name, block.input, decision.scope)
        content = rule_content_for(scope, block.name, block.input)
        if content is None:
            return None
        return [
            PermissionRule(
                tool_name=block.name,
                rule_content=content,
                behavior=PermissionBehavior.ALLOW,
                source="user",
            )
        ]

    def confirm_event(self, decisions: Sequence[ConfirmDecision]) -> UserConfirmResultEvent:
        """把前端的决策组装成框架事件。

        命中的 tool_call 直接从挂起状态里取：客户端只给了 id，
        命令原文与工具名都以服务端这份为准，改不了。
        """
        blocks = {block.id: block for block in self.confirm_blocks()}
        results = [
            ConfirmResult(
                confirmed=decision.confirmed,
                tool_call=blocks[decision.tool_call_id],
                rules=self._rules_for(decision, blocks[decision.tool_call_id]),
            )
            for decision in decisions
            if decision.tool_call_id in blocks
        ]
        if not results:
            raise ValueError("没有匹配的待确认调用")
        reply = self._agent.state.get_reply_message(self._agent.name)
        return UserConfirmResultEvent(
            reply_id=reply.id if reply is not None else "",
            confirm_results=results,
        )

    async def resume(self, decisions: Sequence[ConfirmDecision]) -> AsyncIterator[dict[str, Any]]:
        """带着用户决策从挂起点继续，照常往外吐事件 dict。"""
        async for event in self._agent.reply_stream(self.confirm_event(decisions)):
            yield event_to_dict(event)

    def external_event(self, answers: Sequence[ExternalAnswer]) -> ExternalExecutionResultEvent:
        """把前端答案组装成框架的 external execution 结果。

        问题原文与工具名都以服务端挂起态为准；客户端只回 tool_call_id 与答案。
        """
        blocks = {block.id: block for block in self.external_blocks()}
        results: list[ToolResultBlock] = []
        for answer in answers:
            block = blocks.get(answer.tool_call_id)
            if block is None:
                raise ValueError(f"未知的工具调用: {answer.tool_call_id}")
            params = AskUserParams.model_validate_json(block.input)
            questions = [q.model_dump(mode="json") for q in params.questions]
            normalized = validate_answers(questions, answer.answers)
            meta = AskUserMetadata(answers=[AskUserAnswer(**item) for item in normalized])
            summary = "\n".join(
                f"Q: {item['question']}\nA: {', '.join(item['selected']) or (item['other'] or '')}"
                for item in normalized
            )
            results.append(
                ToolResultBlock(
                    id=block.id,
                    name=block.name,
                    output=summary,
                    state=ToolResultState.SUCCESS,
                    metadata=meta.model_dump(mode="json"),
                )
            )
        if not results:
            raise ValueError("没有匹配的待回答调用")
        reply = self._agent.state.get_reply_message(self._agent.name)
        return ExternalExecutionResultEvent(
            reply_id=reply.id if reply is not None else "",
            execution_results=results,
        )

    async def resume_external(
        self, answers: Sequence[ExternalAnswer]
    ) -> AsyncIterator[dict[str, Any]]:
        """带着用户答案从挂起点继续。"""
        async for event in self._agent.reply_stream(self.external_event(answers)):
            yield event_to_dict(event)

    def interrupt_event(self) -> UserInterruptEvent:
        """构造框架的中断事件，用一个明确的结束代替前端断流。"""
        reply = self._agent.state.get_reply_message(self._agent.name)
        return UserInterruptEvent(reply_id=reply.id if reply is not None else "")

    async def interrupt(self) -> AsyncIterator[dict[str, Any]]:
        """中止挂起中的 reply，照常吐事件 dict。"""
        async for event in self._agent.reply_stream(self.interrupt_event()):
            yield event_to_dict(event)


def build_agentscope_runtime(
    system_prompt: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    profile: PermissionProfile | None = None,
    state: AgentState | None = None,
    web_search: bool | None = None,
) -> AgentScopeRuntime:
    """装配真实运行时（env 提供模型凭据）。

    - 工具：全量内置工具 + ToolGroup，档位只影响权限上下文；
    - 密钥：挂 SecretInjectionMiddleware —— 占位符在工具边界换成真值，
      返回里的真值再抹回占位符，模型全程看不到明文；
    - 权限：注入档位对应的权限上下文（mode + 白名单 + deny_rules）。
    """
    resolved = profile if profile is not None else default_profile()
    base = resolve_workdir(workdir)
    agent = build_agent(
        name="devops",
        system_prompt=system_prompt if system_prompt is not None else build_system_prompt(base),
        model=build_chat_model(enable_search=web_search),
        toolkit=build_full_toolkit(base, middlewares=[build_secret_middleware()]),
        middlewares=[ProfileNoteMiddleware(resolved)],
        state=_state_for(resolved, state),
    )
    return AgentScopeRuntime(agent, profile=resolved)


def session_runtime_factory(base: object) -> Callable[..., Any]:
    """会话工厂：真实运行时按档位重建，Echo / 测试替身直接复用同一实例。

    Echo 是无状态的，复用没有副作用；真实运行时则必须每会话一份，
    否则消息历史与权限规则会在会话之间互相污染。
    """

    def make(
        session_id: str,
        profile: Any,
        state_json: str | None,
        web_search: bool = False,
    ) -> Any:
        if not isinstance(base, AgentScopeRuntime):
            return base
        state = AgentState.model_validate_json(state_json) if state_json else None
        return build_agentscope_runtime(profile=profile, state=state, web_search=web_search)

    return make
