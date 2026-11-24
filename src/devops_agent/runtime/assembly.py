"""Agent 与运行时装配：本包唯一的框架装配入口。

build_agent / build_agentscope_runtime / session_runtime_factory 都从这里出，
调用方（网关、会话注册表、评测）不直接触碰框架类。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ContextConfig, ModelConfig, ReActConfig
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import Toolkit, ToolMiddlewareBase

from devops_agent.config import load_model
from devops_agent.governance.policy import build_permission_context
from devops_agent.governance.profiles import PermissionProfile, default_profile
from devops_agent.model_settings import ModelParams, effective_params
from devops_agent.runtime.chat_model import build_chat_model, build_context_config
from devops_agent.runtime.compression import CompressionMiddleware, CompressionSink
from devops_agent.runtime.profile_note import ProfileNoteMiddleware
from devops_agent.runtime.prompt import build_system_prompt, resolve_workdir
from devops_agent.runtime.scope_runtime import PROFILE_KEY, AgentScopeRuntime
from devops_agent.secrets.middleware import build_secret_middleware
from devops_agent.tools.catalog import build_toolkit_for


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
        fresh = AgentState(
            permission_context=build_permission_context(profile=profile, workdir=workdir)
        )
        fresh.middle_context[PROFILE_KEY] = profile.key
        return fresh
    if state.middle_context.get(PROFILE_KEY) != profile.key:
        state.permission_context = build_permission_context(profile=profile, workdir=workdir)
        state.middle_context[PROFILE_KEY] = profile.key
    return state


def build_agentscope_runtime(
    system_prompt: str | None = None,
    workdir: str | os.PathLike[str] | None = None,
    profile: PermissionProfile | None = None,
    state: AgentState | None = None,
    web_search: bool | None = None,
    params: ModelParams | None = None,
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
    resolved = profile if profile is not None else default_profile()
    base = resolve_workdir(workdir)
    active = params if params is not None else effective_params()
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
    # sink 由运行时持有、中间件写入：一次装配一份，会随会话快照重建。
    compression_sink = CompressionSink()
    compression_mw = CompressionMiddleware(compression_sink)
    agent = build_agent(
        name="devops",
        system_prompt=system_prompt if system_prompt is not None else build_system_prompt(base),
        model=build_chat_model(enable_search=web_search, params=active),
        toolkit=build_toolkit_for(resolved.tools, base, middlewares=toolkit_mws),
        middlewares=[note_mw, compression_mw],
        context_config=build_context_config(active),
        max_iters=active.max_iters,
        max_retries=active.agent_max_retries,
        state=_state_for(resolved, state, base),
    )
    return AgentScopeRuntime(
        agent,
        profile=resolved,
        workdir=base,
        note_middleware=note_mw,
        toolkit_middlewares=toolkit_mws,
        compression_sink=compression_sink,
        web_search=effective_search,
        params=active,
    )


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
        state = AgentState.model_validate_json(state_json) if state_json else None
        return build_agentscope_runtime(
            profile=profile,
            state=state,
            web_search=web_search,
            params=effective_params(session=model_config or {}),
        )

    return make
