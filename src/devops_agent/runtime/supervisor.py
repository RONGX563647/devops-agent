"""父 Agent + 子 Agent 编排 —— 用 TeamPipeline / GoalPipeline。

- TeamPipeline(leader, members=[TeamMember(agent=..., description=...)])
- GoalPipeline(executor, verifier, max_iters=10, max_retries=3)

为何保留这层薄工厂（不是无意义的透传）：
- 框架隔离边界：调用方只依赖本模块的工厂，不散落 `import agentscope`（AGENTS.md §3.1）；
  框架类或签名变更时，改动集中在此处，不扩散到业务与测试。
- 装配单一入口：`build_goal_pipeline` 固化了 max_iters / max_retries 默认值，
  是策略落点；其余函数提供稳定的命名，便于三域装配代码复用。
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
