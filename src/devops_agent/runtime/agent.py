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

import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ModelConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.event import ConfirmResult, UserConfirmResultEvent
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase, OpenAIChatModel
from agentscope.permission import PermissionBehavior, PermissionRule
from agentscope.state import AgentState
from agentscope.tool import Toolkit, ToolMiddlewareBase
from pydantic import SecretStr

from devops_agent.config import RuntimePolicy, load_policy
from devops_agent.governance.policy import build_permission_context
from devops_agent.governance.profiles import PermissionProfile, default_profile
from devops_agent.runtime.confirm import (
    ConfirmDecision,
    effective_scope,
    pending_call,
    rule_content_for,
)
from devops_agent.runtime.events import event_to_dict
from devops_agent.runtime.profile_note import ProfileNoteMiddleware
from devops_agent.secrets.middleware import build_secret_middleware
from devops_agent.tools.catalog import build_toolkit_for, shell_name

DEFAULT_SYSTEM_PROMPT = "你是 DevOps 助手，帮助处理持续交付、项目排期与 Bug 定位。"

# 快照里记录「这份状态是按哪个档位装配的」的键。
# 必须用档位 key 而不是 permission_context.mode 判断：workspace 与 execute
# 的 mode 都是 accept_edits，只比 mode 时切档等于什么都没变（白名单永不装配），
# 反向切回来又会把上一档点「本会话允许」攒下的规则带进新档。
PROFILE_KEY = "permission_profile"

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
2. 不要编造命令输出或文件内容。工具失败就据实说明失败原因；
   同一个工具连续失败两次就换手段（例如 {glob_tool} 与 shell 互换），
   不要反复重试同一种调用。
3. 最终回答用中文，可用 Markdown（标题、列表、表格、代码块）；引用工具输出时保持原样。"""


def resolve_workdir(workdir: str | os.PathLike[str] | None = None) -> Path:
    """确定 Agent 的工作目录：显式参数 > 环境变量 > 进程当前目录。"""
    if workdir is not None:
        return Path(workdir).expanduser().resolve()
    return Path(os.environ.get(WORKDIR_ENV) or os.getcwd()).expanduser().resolve()


# 提示词版本：改动系统提示或档位说明时手动递增，评测批次会记录它，
# 便于把「分数变化」和「哪一版提示词」对上。hash 由 prompt_fingerprint() 现算。
# v2：重试条款改成「同一工具连续失败两次就换手段」，不再指定具体工具名 ——
#     依赖缺失时（如缺 rg）原来的「改用 Grep 重试」会把模型推回同一堵墙。
PROMPT_ID = "agent-system"
PROMPT_VERSION = "v2"


def _render_system_prompt(workdir_label: str) -> str:
    """按给定的工作目录标签渲染系统提示（评测 fingerprint 用占位符）。"""
    tool_prompt = _TOOL_PROMPT.format(
        shell_tool="Bash",
        shell=shell_name(),
        workdir=workdir_label,
        read_tool="Read",
        glob_tool="Glob",
        grep_tool="Grep",
    )
    return "\n\n".join([DEFAULT_SYSTEM_PROMPT, tool_prompt])


def build_system_prompt(workdir: str | os.PathLike[str] | None = None) -> str:
    """拼装系统提示：角色描述 + 可用工具契约。

    **整段与会话无关、逐字节稳定**——这是前缀缓存真正能复用的部分。
    随档位变化的内容（能不能写、越界怎么办）不放在这里：系统提示在请求最前面，
    那里改一个字，后面的工具 schema 与整段历史全部作废（实测跨档命中 21%）。
    档位约束改由 `runtime/profile_note.py` 追加在消息末尾（实测 96%）。
    """
    return _render_system_prompt(str(resolve_workdir(workdir)))


def prompt_fingerprint() -> str:
    """提示词指纹：与具体机器路径无关，用于评测批次对比。

    只覆盖系统提示与显式的 PROMPT_VERSION；档位说明是动态追加的，
    改动 `runtime/profile_note.py` 时需要同步递增 PROMPT_VERSION。
    """
    payload = f"{PROMPT_ID}:{PROMPT_VERSION}\n{_render_system_prompt('<workdir>')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def build_chat_model(enable_search: bool | None = None) -> OpenAIChatModel:
    """从环境变量构造 OpenAI 兼容模型。

    必填 env（缺失抛 RuntimeError，不打印 key）：
    - DEVOPS_AGENT_API_KEY
    - DEVOPS_AGENT_BASE_URL
    - DEVOPS_AGENT_MODEL

    可选 env：
    - DEVOPS_AGENT_ENABLE_SEARCH：设为 1 时给模型请求体加 `enable_search=true`
      （DashScope 兼容接口的联网搜索开关）；默认关闭。
    - DEVOPS_AGENT_SEARCH_MODE：`forced`（默认）连同 `search_options` 一起下发，
      供应商无条件检索并把结果注入上下文；`auto` 只发 `enable_search`，
      由模型自己决定搜不搜（实测多数情况下不搜）。

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
    return OpenAIChatModel(
        credential=credential,
        model=model_name,
        stream=True,
        extra_body=_search_extra_body(enable_search),
    )


def _state_for(
    profile: PermissionProfile,
    state: AgentState | None,
    workdir: Path | None = None,
) -> AgentState:
    """准备跨轮状态：没有就新建；有但档位变了就重建权限上下文。

    档位切换必须丢弃旧的 allow_rules —— 那些规则是在另一个信任级别下点
    「本会话允许」攒的，跨档继续生效等于绕过用户这次的选择。

    判据是 `profile.key` 而不是 `permission_context.mode`：workspace 与
    execute 的 mode 都是 `accept_edits`，比 mode 会得出「没换档」的结论，
    execute 档的白名单从不装配、旧档的 allow_rules 也一并留了下来。

    老快照没有这个键：视为档位不同，重建一次。只换 `permission_context`，
    `context`（对话历史）不动，用户无感。
    """
    if state is None:
        fresh = AgentState(
            permission_context=build_permission_context(profile=profile, workdir=workdir)
        )
        fresh.middle_context[PROFILE_KEY] = profile.key
        return fresh
    if state.middle_context.get(PROFILE_KEY) != profile.key:
        state.permission_context = build_permission_context(profile=profile, workdir=workdir)
        state.middle_context[PROFILE_KEY] = profile.key
    return state


class AgentScopeRuntime:
    """真实 Agent 运行时：把 Agent.reply_stream 适配到 AgentRuntime 协议。"""

    def __init__(
        self,
        agent: Agent,
        *,
        profile: PermissionProfile | None = None,
        workdir: Path | None = None,
        note_middleware: ProfileNoteMiddleware | None = None,
        toolkit_middlewares: Sequence[ToolMiddlewareBase] | None = None,
        web_search: bool = False,
    ) -> None:
        self._agent = agent
        self._profile = profile if profile is not None else default_profile()
        self._workdir = workdir if workdir is not None else resolve_workdir()
        # 热替换要保住这两件装配期的依赖：换 toolkit 时密钥注入不能丢，
        # 换档位时末尾的档位说明必须同步改，否则「切了但模型还按旧档写」。
        self._note_middleware = note_middleware
        self._toolkit_middlewares = list(toolkit_middlewares or [])
        self._web_search = web_search

    @property
    def profile(self) -> PermissionProfile:
        return self._profile

    @property
    def web_search(self) -> bool:
        """这一轮到底联不联网（已解析部署默认）。

        会话注册表要靠它记录「实际生效值」：请求里省略该字段时不能记成 False，
        否则下一轮显式传 False 与「沿用默认」会被当成同一个配置，开关切换失效。
        """
        return self._web_search

    def apply_profile(
        self,
        profile: PermissionProfile,
        *,
        web_search: bool | None = None,
    ) -> None:
        """会话中切档：原地热替换，当前流不中断，下一次判定用新档位。

        **不能整体替换 state.permission_context**：Agent 构造时
        `self._engine = PermissionEngine(self.state.permission_context)` 持有的是
        对象引用；换对象后引擎仍按旧档判定，界面却显示新档。只能逐字段写回。
        旧档攒下的 allow_rules 随这次赋值丢弃，与「切档即换信任级别」一致。

        toolkit 整体换（Agent 每次调用现取 schema），但必须重挂密钥中间件；
        模型只在联网开关真的变化时重建，避免无谓丢掉前缀缓存。
        """
        fresh = build_permission_context(profile=profile, workdir=self._workdir)
        context = self._agent.state.permission_context
        context.mode = fresh.mode
        context.working_directories = fresh.working_directories
        context.allow_rules = fresh.allow_rules
        context.deny_rules = fresh.deny_rules
        context.ask_rules = fresh.ask_rules

        self._agent.toolkit = build_toolkit_for(
            profile.tools,
            self._workdir,
            middlewares=self._toolkit_middlewares or None,
        )
        if self._note_middleware is not None:
            self._note_middleware.set_profile(profile)
        if web_search is not None and web_search != self._web_search:
            self._agent.model = build_chat_model(enable_search=web_search)
            self._web_search = web_search
            # 开关换了，末尾说明也要跟着换：否则检索结果已在上下文里，
            # 模型却仍按「没有联网能力」作答。
            if self._note_middleware is not None:
                self._note_middleware.set_web_search(web_search)
        self._agent.state.middle_context[PROFILE_KEY] = profile.key
        self._profile = profile

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
        """当前仍等待用户确认的 tool_call（权威副本，含原始 input）。"""
        return list(self._agent.state.get_awaiting_tool_calls(self._agent.name))

    def pending_calls(self) -> list[dict[str, Any]]:
        """给前端展示的精简数组（不含可回传的完整块）。"""
        return [
            asdict(pending_call(block.model_dump(mode="json"))) for block in self.awaiting_blocks()
        ]

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
        blocks = {block.id: block for block in self.awaiting_blocks()}
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


def build_agentscope_runtime(
    system_prompt: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    profile: PermissionProfile | None = None,
    state: AgentState | None = None,
    web_search: bool | None = None,
) -> AgentScopeRuntime:
    """装配真实运行时（env 提供模型凭据）。

    - 工具：按档位装配（只读档四件套，放开档追加 Write/Edit）；
    - 密钥：挂 SecretInjectionMiddleware —— 占位符在工具边界换成真值，
      返回里的真值再抹回占位符，模型全程看不到明文；
    - 权限：注入档位对应的权限上下文（mode + 白名单 + deny_rules）。
    """
    resolved = profile if profile is not None else default_profile()
    base = resolve_workdir(workdir)
    # 先定下这一轮到底联不联网：模型请求体与末尾说明必须同源，
    # 否则会出现「请求里带了检索结果、提示词却说没联网」的错位。
    effective_search = (
        web_search
        if web_search is not None
        else os.environ.get("DEVOPS_AGENT_ENABLE_SEARCH") == "1"
    )
    secret_mw = build_secret_middleware()
    note_mw = ProfileNoteMiddleware(resolved, web_search=effective_search)
    toolkit_mws: list[ToolMiddlewareBase] = [secret_mw]
    agent = build_agent(
        name="devops",
        system_prompt=system_prompt if system_prompt is not None else build_system_prompt(base),
        model=build_chat_model(enable_search=web_search),
        toolkit=build_toolkit_for(resolved.tools, base, middlewares=toolkit_mws),
        middlewares=[note_mw],
        state=_state_for(resolved, state, base),
    )
    return AgentScopeRuntime(
        agent,
        profile=resolved,
        workdir=base,
        note_middleware=note_mw,
        toolkit_middlewares=toolkit_mws,
        web_search=effective_search,
    )


def session_runtime_factory(base: object) -> Callable[..., Any]:
    """会话工厂：真实运行时按档位重建，Echo / 测试替身直接复用同一实例。

    Echo 是无状态的，复用没有副作用；真实运行时则必须每会话一份，
    否则消息历史与权限规则会在会话之间互相污染。
    """

    def make(
        session_id: str,
        profile: Any,
        state_json: str | None,
        web_search: bool | None = None,
    ) -> Any:
        if not isinstance(base, AgentScopeRuntime):
            return base
        state = AgentState.model_validate_json(state_json) if state_json else None
        return build_agentscope_runtime(profile=profile, state=state, web_search=web_search)

    return make
