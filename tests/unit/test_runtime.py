"""编排层工厂单元测试（用 MagicMock 作为 model，不发真实模型请求）。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agentscope.agent import Agent
from agentscope.pipeline import GoalPipeline, TeamMember, TeamPipeline

from devops_agent.config import ModelPolicy, ReactPolicy, RuntimePolicy
from devops_agent.governance.profiles import PROFILES, default_profile
from devops_agent.runtime.agent import (
    PROFILE_KEY,
    WORKDIR_ENV,
    _state_for,
    build_agent,
    build_agentscope_runtime,
    build_chat_model,
    build_system_prompt,
    prompt_fingerprint,
    resolve_workdir,
)
from devops_agent.runtime.supervisor import (
    as_member,
    build_goal_pipeline,
    build_team_pipeline,
)


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


def test_prompt_fingerprint_ignores_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评测批次要能跨机器比较，指纹不能把绝对工作目录算进去。"""
    baseline = prompt_fingerprint()
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path))
    assert prompt_fingerprint() == baseline
    assert len(baseline) == 64


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


def test_build_agentscope_runtime_wires_profile_toolkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认档（workspace）必须挂上写工具，且权限 mode 与该档位一致。

    以前这里断言的是「没有 Write」——那正是「工作区可写却写不了文件」的成因：
    说明里承诺能建/删/改，工具集里却只有只读四件套。
    """
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")

    runtime = build_agentscope_runtime(workdir=tmp_path)
    agent = runtime._agent
    names = {schema["function"]["name"] for schema in asyncio.run(agent.toolkit.get_tool_schemas())}
    assert {"Bash", "Read", "Glob", "Grep"} <= names
    assert {"Write", "Edit"} <= names
    assert agent.state.permission_context.mode.value == default_profile().mode


def test_apply_profile_hot_swaps_context_toolkit_and_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话中切档必须原地改：引擎持有 context 引用，换对象等于没切。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    monkeypatch.delenv("DEVOPS_AGENT_ENABLE_SEARCH", raising=False)

    runtime = build_agentscope_runtime(workdir=tmp_path, profile=PROFILES["readonly"])
    agent = runtime._agent
    context = agent.state.permission_context
    assert agent._engine.context is context
    before = asyncio.run(agent.toolkit.get_tool_schemas())
    assert all(schema["function"]["name"] not in {"Write", "Edit"} for schema in before)

    # 模拟旧档攒下的「本会话允许」，切档必须连同信任级别一起丢弃
    context.allow_rules["Bash"] = []
    runtime.apply_profile(PROFILES["workspace"])

    assert agent.state.permission_context is context
    assert agent._engine.context is context
    assert context.mode.value == "accept_edits"
    assert context.allow_rules == {}
    assert runtime.profile.key == "workspace"
    assert agent.state.middle_context[PROFILE_KEY] == "workspace"
    names = {schema["function"]["name"] for schema in asyncio.run(agent.toolkit.get_tool_schemas())}
    assert {"Write", "Edit"} <= names

    # 档位说明同步换：否则模型仍按「只读」回答，界面却显示已切到可写
    seen: dict[str, object] = {}

    async def nxt(**kwargs: object) -> str:
        seen.update(kwargs)
        return "ok"

    asyncio.run(runtime._note_middleware.on_model_call(agent, {"messages": []}, nxt))  # type: ignore[arg-type]
    tail = seen["messages"][-1].get_text_content()  # type: ignore[index]
    assert "工作区可写" in tail


def test_apply_profile_can_hot_swap_web_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """联网开关在模型请求体里，切档端点也要能原地换模型实例。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    monkeypatch.delenv("DEVOPS_AGENT_ENABLE_SEARCH", raising=False)

    runtime = build_agentscope_runtime(workdir=tmp_path)
    assert runtime._agent.model.extra_body is None
    runtime.apply_profile(PROFILES["execute"], web_search=True)
    assert runtime._agent.model.extra_body == {"enable_search": True}
    runtime.apply_profile(PROFILES["execute"], web_search=False)
    assert runtime._agent.model.extra_body is None


def test_state_rebuilds_on_profile_switch_even_with_same_mode() -> None:
    """workspace 与 execute 的 mode 都是 accept_edits，切档必须按 key 判定。

    以前比的是 `permission_context.mode`，于是 workspace → execute 被当成
    「没换档」：白名单从不装配，可执行档跟工作区档一模一样。
    """
    state = _state_for(PROFILES["workspace"], None)
    assert state.middle_context[PROFILE_KEY] == "workspace"
    assert state.permission_context.allow_rules == {}  # 工作区档没有命令白名单

    switched = _state_for(PROFILES["execute"], state)
    assert switched is state  # 只换权限上下文，对话历史不动
    assert switched.middle_context[PROFILE_KEY] == "execute"
    prefixes = [rule.rule_content for rule in switched.permission_context.allow_rules["Bash"]]
    assert "pytest:*" in prefixes

    # 切回工作区：白名单要撤掉，不能把上一档的规则带过来
    back = _state_for(PROFILES["workspace"], switched)
    assert back.permission_context.allow_rules == {}


def test_same_profile_keeps_session_allow_rules() -> None:
    """同一档位重复装配不能重建上下文，否则「本会话允许」每轮都被清掉。"""
    state = _state_for(PROFILES["workspace"], None)
    before = state.permission_context
    again = _state_for(PROFILES["workspace"], state)
    assert again.permission_context is before
