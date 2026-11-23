"""编排层工厂单元测试（用 MagicMock 作为 model，不发真实模型请求）。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agentscope.agent import Agent
from agentscope.pipeline import GoalPipeline, TeamMember, TeamPipeline

from devops_agent.config import ModelPolicy, ReactPolicy, RuntimePolicy
from devops_agent.governance.profiles import default_profile
from devops_agent.runtime.agent import (
    WORKDIR_ENV,
    build_agent,
    build_agentscope_runtime,
    build_chat_model,
    build_system_prompt,
    resolve_workdir,
)
from devops_agent.runtime.supervisor import (
    as_member,
    build_goal_pipeline,
    build_team_pipeline,
)
from devops_agent.tools.catalog import DEFAULT_GROUPS, activated_groups


def _agent(name: str) -> Agent:
    return build_agent(name=name, system_prompt="sys", model=MagicMock())


def test_build_agent_wires_config() -> None:
    a = _agent("a")
    assert isinstance(a, Agent)
    assert a.name == "a"


def test_build_agent_middlewares_and_limits() -> None:
    a = build_agent(
        name="b",
        system_prompt="sys",
        model=MagicMock(),
        middlewares=[],
        max_iters=3,
        max_retries=5,
    )
    assert isinstance(a, Agent)


def test_as_member() -> None:
    m = as_member(_agent("b"), "项目排期")
    assert isinstance(m, TeamMember)


def test_build_team_pipeline() -> None:
    leader = _agent("leader")
    members = [as_member(_agent("delivery"), "可持续交付"), as_member(_agent("debug"), "Bug 定位")]
    team = build_team_pipeline(leader, members)
    assert isinstance(team, TeamPipeline)


def test_build_goal_pipeline() -> None:
    gp = build_goal_pipeline(_agent("exec"), _agent("verify"), max_iters=4, max_retries=2)
    assert isinstance(gp, GoalPipeline)


def test_build_chat_model_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DEVOPS_AGENT_API_KEY", "DEVOPS_AGENT_BASE_URL", "DEVOPS_AGENT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="缺少环境变量"):
        build_chat_model()


def test_build_chat_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    from agentscope.model import OpenAIChatModel

    assert isinstance(build_chat_model(), OpenAIChatModel)


def test_build_chat_model_search_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """联网搜索默认关闭：不改变既有请求体。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    monkeypatch.delenv("DEVOPS_AGENT_ENABLE_SEARCH", raising=False)
    assert build_chat_model().extra_body is None


def test_build_chat_model_search_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式开启时注入 enable_search=true，由供应商侧执行联网检索。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    monkeypatch.setenv("DEVOPS_AGENT_ENABLE_SEARCH", "1")
    assert build_chat_model().extra_body == {"enable_search": True}


def test_build_chat_model_request_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐请求开关优先于环境变量：前端「联网」按钮不必重启进程。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    monkeypatch.delenv("DEVOPS_AGENT_ENABLE_SEARCH", raising=False)

    assert build_chat_model(enable_search=True).extra_body == {"enable_search": True}
    assert build_chat_model(enable_search=False).extra_body is None


def test_build_agent_reads_policy_defaults() -> None:
    policy = RuntimePolicy(
        react=ReactPolicy(default=7, delivery=9),
        model=ModelPolicy(max_retries=5),
    )
    general = build_agent("schedule", "sys", MagicMock(), policy=policy)
    delivery = build_agent("delivery", "sys", MagicMock(), policy=policy)
    assert general.react_config.max_iters == 7
    assert delivery.react_config.max_iters == 9
    assert general.model_config.max_retries == 5


def test_build_agent_explicit_limits_override_policy() -> None:
    policy = RuntimePolicy(react=ReactPolicy(default=7), model=ModelPolicy(max_retries=5))
    agent = build_agent("schedule", "sys", MagicMock(), max_iters=2, max_retries=1, policy=policy)
    assert agent.react_config.max_iters == 2
    assert agent.model_config.max_retries == 1


def test_build_agent_injects_permission_state() -> None:
    """state 必须透传到 Agent：权限上下文只能靠注入生效，Agent 内部会用它建引擎。"""
    from agentscope.permission import PermissionContext, PermissionMode
    from agentscope.state import AgentState

    agent = build_agent(
        "devops",
        "sys",
        MagicMock(),
        state=AgentState(permission_context=PermissionContext(mode=PermissionMode.DONT_ASK)),
    )
    assert agent.state.permission_context.mode == PermissionMode.DONT_ASK


def test_build_system_prompt_declares_tools(tmp_path: Path) -> None:
    """系统提示必须写明可用工具与工作目录，否则模型会只在正文里「描述」工具调用。"""
    prompt = build_system_prompt(tmp_path)
    for token in ("Bash", "Read", "Glob", "Grep", str(tmp_path.resolve())):
        assert token in prompt
    assert "必须真的发起工具调用" in prompt


def test_system_prompt_is_profile_independent(tmp_path: Path) -> None:
    """系统提示必须与档位无关：那里改一个字，工具 schema 与整段历史全部缓存失效。"""
    from devops_agent.governance.profiles import resolve
    from devops_agent.runtime.profile_note import policy_note

    prompt = build_system_prompt(tmp_path)
    # 档位说明不在系统提示里
    assert "本会话权限" not in prompt
    assert "Write" not in prompt
    # 但两档的注记必须不同，否则切档等于没切
    readonly_note = policy_note(resolve("readonly"))
    workspace_note = policy_note(resolve("workspace"))
    assert readonly_note != workspace_note
    assert "只读" in readonly_note


def test_profile_note_middleware_appends_trailing_message(tmp_path: Path) -> None:
    """档位注记追加在 messages 末尾：前缀全部保留，变的只有尾部。"""
    import asyncio

    from agentscope.message import UserMsg

    from devops_agent.governance.profiles import resolve
    from devops_agent.runtime.profile_note import ProfileNoteMiddleware

    mw = ProfileNoteMiddleware(resolve("readonly"))
    history = [UserMsg(name="user", content="第一问"), UserMsg(name="assistant", content="第一答")]
    seen: dict[str, object] = {}

    async def nxt(**kwargs: object) -> str:
        seen.update(kwargs)
        return "ok"

    asyncio.run(mw.on_model_call(agent=None, input_kwargs={"messages": history}, next_handler=nxt))  # type: ignore[arg-type]
    sent = seen["messages"]
    assert isinstance(sent, list)
    assert len(sent) == len(history) + 1
    assert "本会话权限" in str(sent[-1].get_text_content())
    # 原列表不能被就地改写：那会污染会话历史，下一轮重复累积
    assert len(history) == 2


def test_resolve_workdir_prefers_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path / "from-env"))
    assert resolve_workdir(tmp_path) == tmp_path.resolve()


def test_resolve_workdir_reads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path))
    assert resolve_workdir() == tmp_path.resolve()


def test_build_agentscope_runtime_wires_full_toolkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """所有档位共用全量工具；默认激活写/任务组，权限由 mode 控制。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")

    runtime = build_agentscope_runtime(workdir=tmp_path)
    agent = runtime._agent
    names = {schema["function"]["name"] for schema in asyncio.run(agent.toolkit.get_tool_schemas())}
    assert {"Bash", "Read", "Glob", "Grep", "AskUser"} <= names
    # 写工具已注册，但默认 schema 只列 basic 组；激活后可见
    assert {group.name for group in agent.toolkit.tool_groups} >= {"write", "task"}
    assert set(agent.state.tool_context.activated_groups) == set(DEFAULT_GROUPS)
    assert agent.state.permission_context.mode.value == default_profile().mode


def test_state_for_switches_permission_context_with_same_mode() -> None:
    """workspace → execute 的 mode 都是 accept_edits，白名单仍必须重新注入。"""
    from devops_agent.governance.profiles import PROFILES
    from devops_agent.runtime.agent import _state_for

    state = _state_for(PROFILES["workspace"], None)
    assert state.tool_context.activated_groups == activated_groups()
    assert state.permission_context.allow_rules == {}

    switched = _state_for(PROFILES["execute"], state)
    rules = switched.permission_context.allow_rules.get("Bash", [])
    prefixes = [rule.rule_content for rule in rules]
    assert any(str(prefix).endswith(":*") for prefix in prefixes)

    # 切回 workspace 必须丢掉 execute 的白名单，避免跨档规则继续生效
    back = _state_for(PROFILES["workspace"], switched)
    assert back.permission_context.allow_rules == {}
