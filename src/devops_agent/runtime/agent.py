"""Agent 装配兼容门面。

实现已按职责拆到同包内：
- `runtime/prompt.py`：系统提示词与三类指纹；
- `runtime/chat_model.py`：模型与上下文配置构造；
- `runtime/scope_runtime.py`：AgentScopeRuntime（框架事件 → dict）；
- `runtime/assembly.py`：build_agent / build_agentscope_runtime / 会话工厂。

本模块只保留原导入路径，`from devops_agent.runtime.agent import ...` 不变。
"""

from __future__ import annotations

from devops_agent.runtime.assembly import (
    _state_for,
    build_agent,
    build_agentscope_runtime,
    session_runtime_factory,
)
from devops_agent.runtime.chat_model import (
    _SEARCH_MODE_AUTO,
    _SEARCH_MODE_FORCED,
    _SEARCH_OPTIONS,
    MODEL_WIRE_KEYS,
    SEARCH_MODE_ENV,
    _search_extra_body,
    build_chat_model,
    build_context_config,
)
from devops_agent.runtime.prompt import (
    _TOOL_PROMPT,
    DEFAULT_SYSTEM_PROMPT,
    PROMPT_ID,
    PROMPT_VERSION,
    WORKDIR_ENV,
    _render_system_prompt,
    build_system_prompt,
    profile_note_sha,
    prompt_bytes_sha,
    prompt_factors,
    prompt_fingerprint,
    resolve_workdir,
)
from devops_agent.runtime.scope_runtime import PROFILE_KEY, AgentScopeRuntime
from devops_agent.tools.catalog import shell_name

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "MODEL_WIRE_KEYS",
    "PROFILE_KEY",
    "PROMPT_ID",
    "PROMPT_VERSION",
    "SEARCH_MODE_ENV",
    "WORKDIR_ENV",
    "AgentScopeRuntime",
    "_SEARCH_MODE_AUTO",
    "_SEARCH_MODE_FORCED",
    "_SEARCH_OPTIONS",
    "_TOOL_PROMPT",
    "_render_system_prompt",
    "_search_extra_body",
    "_state_for",
    "build_agent",
    "build_agentscope_runtime",
    "build_chat_model",
    "build_context_config",
    "build_system_prompt",
    "profile_note_sha",
    "prompt_bytes_sha",
    "prompt_factors",
    "prompt_fingerprint",
    "resolve_workdir",
    "session_runtime_factory",
    "shell_name",
]
