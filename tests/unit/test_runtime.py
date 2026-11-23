"""编排层工厂单元测试（用 MagicMock 作为 model，不发真实模型请求）。"""

from unittest.mock import MagicMock

import pytest
from agentscope.agent import Agent
from agentscope.pipeline import GoalPipeline, TeamMember, TeamPipeline

from devops_agent.config import ModelPolicy, ReactPolicy, RuntimePolicy
from devops_agent.runtime.agent import build_agent
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
    assert EchoRuntime().run("s1", "hi") == "[s1] hi"


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


def test_build_agent_fallback_model_requires_explicit_model() -> None:
    policy = RuntimePolicy(model=ModelPolicy(fallback_model="some-model"))
    with pytest.raises(ValueError, match="fallback_model"):
        build_agent("schedule", "sys", MagicMock(), policy=policy)
