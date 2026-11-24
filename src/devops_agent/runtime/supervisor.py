"""父 Agent + 子 Agent 编排 —— 用 TeamPipeline / GoalPipeline。

- TeamPipeline(leader, members=[TeamMember(agent=..., description=...)])
- GoalPipeline(executor, verifier, max_iters=..., max_retries=...)

为何保留这层薄工厂（不是无意义的透传）：
- 框架隔离边界：调用方只依赖本模块的工厂，不散落 `import agentscope`（AGENTS.md §3.1）；
  框架类或签名变更时，改动集中在此处，不扩散到业务与测试。
- 装配单一入口：`build_goal_pipeline` 的 max_iters / max_retries 默认值来自
  `config/model.yaml`（此前是硬编码 10 / 3，和 yaml 里的 12 / 3 不一致），
  是策略落点；其余函数提供稳定的命名，便于三域装配代码复用。
"""

from __future__ import annotations

from agentscope.agent import Agent
from agentscope.pipeline import GoalPipeline, TeamMember, TeamPipeline

from devops_agent.config import load_model


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
    max_iters: int | None = None,
    max_retries: int | None = None,
) -> GoalPipeline:
    """装配执行/校验双 Agent 的目标流水线。

    默认值来自配置而不是本文件：硬编码的 10/3 和 `policy.yaml` 的 12/3
    长期不一致，正是「同一个参数两个值」的典型。None 表示「用部署默认」。
    """
    settings = load_model()
    return GoalPipeline(
        executor=executor,
        verifier=verifier,
        max_iters=max_iters if max_iters is not None else settings.max_iters_for("default"),
        max_retries=(
            max_retries if max_retries is not None else settings.agent_max_retries
        ),
    )
