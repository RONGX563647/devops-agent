"""Agent 与运行时装配：本包唯一的框架装配入口。

build_agent / build_agentscope_runtime / session_runtime_factory 都从这里出，
调用方（网关、会话注册表、评测）不直接触碰框架类。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ContextConfig, ModelConfig, ReActConfig
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit, ToolMiddlewareBase

from devops_agent.config import load_memory, load_model
from devops_agent.governance.compile import compile_context
from devops_agent.governance.model import PermissionProfile
from devops_agent.governance.presets import default_profile
from devops_agent.memory.observer import MemoryMiddleware
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.snapshot import record_restore_failure
from devops_agent.model_settings import ModelParams, effective_params
from devops_agent.runtime.chat_model import build_chat_model, build_context_config
from devops_agent.runtime.compression import CompressionMiddleware, CompressionSink
from devops_agent.runtime.profile_note import ProfileNoteMiddleware
from devops_agent.runtime.prompt import build_system_prompt, resolve_workdir
from devops_agent.runtime.scope_runtime import PROFILE_KEY, AgentScopeRuntime
from devops_agent.search_mode import search_mode
from devops_agent.secrets.middleware import build_secret_middleware
from devops_agent.tools.catalog import build_toolkit_for
from devops_agent.tools.names import SEARCH_TOOL_NAME
from devops_agent.tools.web_search import WebSearchTool, build_backend


def build_agent(
    name: str,
    system_prompt: str,
    model: ChatModelBase,
    toolkit: Toolkit | None = None,
    middlewares: Sequence[MiddlewareBase] | None = None,
    max_iters: int | None = None,
    max_retries: int | None = None,
    context_config: ContextConfig | None = None,
    state: AgentState | None = None,
) -> Agent:
    """按域装配一个 Agent（交付 / 排期 / 定位共用此工厂）。

    max_iters / max_retries 省略时读 `config/model.yaml`；
    max_iters 按域取 `max_iters.<name>`，未命中回退 `default`。
    state 用于注入权限上下文等跨轮状态（省略则 Agent 自建默认状态）。
    """
    settings = load_model()
    return Agent(
        name=name,
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        middlewares=list(middlewares) if middlewares else None,
        state=state,
        context_config=context_config,
        react_config=ReActConfig(
            max_iters=max_iters if max_iters is not None else settings.max_iters_for(name)
        ),
        model_config=ModelConfig(
            max_retries=(max_retries if max_retries is not None else settings.agent_max_retries),
        ),
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
        fresh = AgentState(permission_context=compile_context(profile=profile, workdir=workdir))
        fresh.middle_context[PROFILE_KEY] = profile.key
        return fresh
    if state.middle_context.get(PROFILE_KEY) != profile.key:
        state.permission_context = compile_context(profile=profile, workdir=workdir)
        state.middle_context[PROFILE_KEY] = profile.key
    return state


@dataclass(frozen=True)
class RuntimeParts:
    """装配期的分步产物：每步只补齐自己负责的字段，最后一次性交给运行时。

    `note_mw` / `toolkit_mws` / `compression_sink` / `agent` 由后续步骤填充，
    因此先留空 —— 这样「解析输入 → 建中间件 → 建 Agent」才能各成一步。
    """

    profile: PermissionProfile
    workdir: Path
    params: ModelParams
    web_search: bool
    system_prompt: str
    state: AgentState
    note_mw: ProfileNoteMiddleware | None = None
    # 联网检索工具实例：预算要跨轮 reset，所以必须由运行时与 toolkit 共用同一个对象。
    search_tool: WebSearchTool | None = None
    toolkit_mws: list[ToolMiddlewareBase] = field(default_factory=list)
    compression_sink: CompressionSink | None = None
    # 三层记忆：只给真实运行时装配；Echo / 测试替身保持无记忆
    memory: MemoryRuntime | None = None
    agent: Agent | None = None


def _resolve_runtime_inputs(
    system_prompt: str | None,
    workdir: str | os.PathLike[str] | None,
    profile: PermissionProfile | None,
    state: AgentState | None,
    web_search: bool | None,
    params: ModelParams | None,
) -> RuntimeParts:
    """解析装配输入：系统提示、工作目录、档位、跨轮状态、参数与联网开关。

    联网开关在这里定为最终值（而不是建模型时再读 env）：模型请求体与末尾
    档位说明必须同源，否则会出现「请求里带了检索结果、提示词却说没联网」。
    优先级：请求里的逐轮开关 > 档位声明 `net` > 部署默认 env。
    `params` 省略时按「会话覆盖 > 部署默认 > yaml > env」解析。
    """
    resolved = profile if profile is not None else default_profile()
    base = resolve_workdir(workdir)
    if web_search is not None:
        effective_search = web_search
    elif resolved.net is not None:
        effective_search = resolved.net
    else:
        effective_search = os.environ.get("DEVOPS_AGENT_ENABLE_SEARCH") == "1"
    return RuntimeParts(
        profile=resolved,
        workdir=base,
        params=params if params is not None else effective_params(),
        web_search=effective_search,
        system_prompt=system_prompt if system_prompt is not None else build_system_prompt(base),
        state=_state_for(resolved, state, base),
    )


def _build_search_tool(parts: RuntimeParts) -> RuntimeParts:
    """按需建检索工具：只有「本轮要联网 + tool 模式 + 后端可用」三者同时成立才建。

    任一不成立就不注册工具 —— 前端据此把按钮置灰并说明原因，而不是给一个
    点了没反应的按钮。`provider` 模式下检索走请求体，也不建工具。
    """
    if not parts.web_search or search_mode() != "tool":
        return parts
    backend, _reason = build_backend()
    if backend is None:
        return parts
    return replace(parts, search_tool=WebSearchTool(backend))


def _build_middlewares(
    parts: RuntimeParts, session_id: str = "", memory_policy: Any | None = None
) -> RuntimeParts:
    """建工具中间件、档位说明、压缩 sink 与记忆运行时（随会话快照重建）。

    `memory_policy` 是评测变体的入口：省略时读全局 `load_memory()`，
    这样线上行为不变，只有评测传入 off/short/full 等变体。
    """
    policy = memory_policy if memory_policy is not None else load_memory()
    memory = MemoryRuntime(session_id, policy) if session_id and policy.enabled else None
    return replace(
        parts,
        note_mw=ProfileNoteMiddleware(parts.profile, web_search=parts.web_search),
        toolkit_mws=[build_secret_middleware()],
        compression_sink=CompressionSink(),
        memory=memory,
    )


def _build_agent(parts: RuntimeParts) -> RuntimeParts:
    """按档位建 Agent：工具、密钥中间件、档位说明与压缩钩子一并挂上。"""
    note_mw = parts.note_mw
    sink = parts.compression_sink
    assert note_mw is not None and sink is not None
    # 记忆中间件放在档位说明之后：请求尾部顺序才是「档位 → 中期 → 长期」
    memory_mws = [MemoryMiddleware(parts.memory)] if parts.memory is not None else []
    # 联网工具不在 profile.tools 里：它由独立的 net 维度授予，只有这一刻才拼进来。
    names = parts.profile.tools + ((SEARCH_TOOL_NAME,) if parts.search_tool is not None else ())
    extra = (
        {SEARCH_TOOL_NAME: parts.search_tool.as_function()}
        if parts.search_tool is not None
        else None
    )
    agent = build_agent(
        name="devops",
        system_prompt=parts.system_prompt,
        model=build_chat_model(enable_search=parts.web_search, params=parts.params),
        toolkit=build_toolkit_for(
            names,
            parts.workdir,
            middlewares=parts.toolkit_mws,
            scope=parts.profile.scope,
            extra=extra,
        ),
        middlewares=[
            note_mw,
            *memory_mws,
            CompressionMiddleware(sink, parts.memory),
        ],
        context_config=build_context_config(parts.params),
        max_iters=parts.params.max_iters,
        max_retries=parts.params.agent_max_retries,
        state=parts.state,
    )
    return replace(parts, agent=agent)


def _wrap_runtime(parts: RuntimeParts) -> AgentScopeRuntime:
    """把分步产物收口成对外的运行时对象（中间件与参数由运行时持有）。"""
    agent = parts.agent
    assert agent is not None
    return AgentScopeRuntime(
        agent,
        profile=parts.profile,
        workdir=parts.workdir,
        note_middleware=parts.note_mw,
        toolkit_middlewares=parts.toolkit_mws,
        compression_sink=parts.compression_sink,
        memory=parts.memory,
        web_search=parts.web_search,
        search_tool=parts.search_tool,
        params=parts.params,
    )


def build_agentscope_runtime(
    system_prompt: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    profile: PermissionProfile | None = None,
    state: AgentState | None = None,
    web_search: bool | None = None,
    params: ModelParams | None = None,
    session_id: str = "",
    memory_policy: Any | None = None,
) -> AgentScopeRuntime:
    """装配真实运行时（env 提供模型凭据）。

    - 工具：按档位装配（只读档四件套，放开档追加 Write/Edit）；
    - 密钥：挂 SecretInjectionMiddleware —— 占位符在工具边界换成真值，
      返回里的真值再抹回占位符，模型全程看不到明文；
    - 权限：注入档位对应的权限上下文（mode + 白名单 + deny_rules）。
    - 上下文压缩：挂 CompressionMiddleware，把「检查 / 触发 / 前后 token」
      变成 SSE 帧 —— 框架的 on_compress_context 钩子没有事件通道。
    - 模型参数：`params` 省略时按「会话覆盖 > 部署默认 > yaml > env」解析。
      `context_config` 与 `context_size` 必须与之一致，否则会出现
      「窗口是新的、压缩阈值还是旧的」。
    """
    # 1) 解析输入：提示词、目录、档位、跨轮状态、参数与本轮联网开关
    parts = _resolve_runtime_inputs(system_prompt, workdir, profile, state, web_search, params)
    # 2) 联网工具：只有 tool 模式且后端可用才建，预算实例要交给运行时持有
    parts = _build_search_tool(parts)
    # 3) 建中间件、压缩 sink 与记忆运行时（无 session_id 时不装配记忆）
    parts = _build_middlewares(parts, session_id, memory_policy)
    # 4) 建 Agent：工具按档位装配，模型参数与上下文窗口同源
    parts = _build_agent(parts)
    # 5) 包成运行时：中间件、sink 与当前参数交给运行时持有
    return _wrap_runtime(parts)


def session_runtime_factory(base: object) -> Callable[..., Any]:
    """会话工厂：真实运行时按档位重建，Echo / 测试替身直接复用同一实例。

    Echo 是无状态的，复用没有副作用；真实运行时则必须每会话一份，
    否则消息历史与权限规则会在会话之间互相污染。

    `model_config` 是该会话的参数覆盖（来自 DB，`None` 表示没有）：
    装配时按「覆盖 > 部署默认 > yaml > env」解析，重建后仍是同一份参数。
    """

    def make(
        session_id: str,
        profile: Any,
        state_json: str | None,
        web_search: bool | None = None,
        model_config: dict[str, Any] | None = None,
    ) -> Any:
        if not isinstance(base, AgentScopeRuntime):
            return base
        state = _restore_state(session_id, state_json)
        return build_agentscope_runtime(
            profile=profile,
            state=state,
            web_search=web_search,
            params=effective_params(session=model_config or {}),
            session_id=session_id,
        )

    return make


def _restore_state(session_id: str, state_json: str | None) -> AgentState | None:
    """解析会话快照；坏快照退回新会话，但必须留痕。

    直接抛会让整个会话建不起来（用户看到 500）；静默吞掉则会「历史莫名清空」，
    排障时连一句日志都没有。折中是记 warning + 计数，`/health` 能看到
    `memory.restores.failed` 在涨（SPEC-0013 §18.2 M5）。
    """
    if not state_json:
        return None
    try:
        return AgentState.model_validate_json(state_json)
    except Exception as exc:  # noqa: BLE001 - 坏快照只能按新会话处理
        record_restore_failure(session_id, exc)
        return None
