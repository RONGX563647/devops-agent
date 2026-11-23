"""编排层工厂单元测试（用 MagicMock 作为 model，不发真实模型请求）。"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agentscope.agent import Agent
from agentscope.message import Msg, TextBlock
from agentscope.pipeline import GoalPipeline, TeamMember, TeamPipeline

from devops_agent.config import ModelPolicy, ReactPolicy, RuntimePolicy, load_policy
from devops_agent.runtime.agent import (
    WORKDIR_ENV,
    AgentScopeRuntime,
    build_agent,
    build_agentscope_runtime,
    build_chat_model,
    build_system_prompt,
    resolve_workdir,
    text_from_msg,
)
from devops_agent.runtime.runtime import EchoRuntime
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


def test_echo_runtime() -> None:
    assert asyncio.run(EchoRuntime().run("s1", "hi")) == "[s1] hi"


def test_text_from_msg_extracts_text_only() -> None:
    msg = Msg(
        name="assistant",
        content=[
            {"type": "thinking", "thinking": "内部推理", "id": "t1", "created_at": "x"},
            TextBlock(type="text", text="你好", id="b1", created_at="x"),
            TextBlock(type="text", text="世界", id="b2", created_at="x"),
        ],
        role="assistant",
    )
    assert text_from_msg(msg) == "你好世界"


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


def test_agentscope_runtime_run() -> None:
    """用假 Agent 验证异步适配：await reply → 提取纯文本。"""

    class FakeAgent:
        async def reply(self, msg: Msg) -> Msg:
            return Msg(
                name="assistant",
                content=[TextBlock(type="text", text="已处理", id="b1", created_at="x")],
                role="assistant",
            )

    runtime = AgentScopeRuntime(FakeAgent())  # type: ignore[arg-type]
    assert asyncio.run(runtime.run("s1", "hello")) == "已处理"


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


def test_resolve_workdir_prefers_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path / "from-env"))
    assert resolve_workdir(tmp_path) == tmp_path.resolve()


def test_resolve_workdir_reads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path))
    assert resolve_workdir() == tmp_path.resolve()


def test_build_agentscope_runtime_wires_readonly_toolkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装配点必须真的把只读工具集与策略权限交给 Agent —— 修复前这里是空的。"""
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")

    runtime = build_agentscope_runtime(workdir=tmp_path)
    agent = runtime._agent
    names = {schema["function"]["name"] for schema in asyncio.run(agent.toolkit.get_tool_schemas())}
    assert {"Bash", "Read", "Glob", "Grep"} <= names
    assert "Write" not in names
    assert agent.state.permission_context.mode.value == load_policy().permission.mode
