"""父 Agent + 子 Agent 编排 —— 用 TeamPipeline / GoalPipeline。

- TeamPipeline(leader, members=[TeamMember(agent=..., description=...)])
- GoalPipeline(executor, verifier, max_iters=10, max_retries=3)
"""

from __future__ import annotations

from agentscope.agent import Agent
from agentscope.pipeline import GoalPipeline, TeamMember, TeamPipeline


def as_member(agent: Agent, description: str) -> TeamMember:
    """把一个域 Agent 包装成 TeamMember。"""
    return TeamMember(agent=agent, description=description)


def build_team_pipeline(leader: Agent, members: list[TeamMember]) -> TeamPipeline:
    """把三个能力域的 Agent 装配为团队流水线。"""
    return TeamPipeline(leader=leader, members=members)


def build_goal_pipeline(
    executor: Agent,
    verifier: Agent,
    *,
    max_iters: int = 10,
    max_retries: int = 3,
) -> GoalPipeline:
    """装配执行/校验双 Agent 的目标流水线。"""
    return GoalPipeline(
        executor=executor,
        verifier=verifier,
        max_iters=max_iters,
        max_retries=max_retries,
    )
