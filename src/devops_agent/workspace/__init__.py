"""工作区领域（SPEC-0021）—— 一工作区一代码库，共享长期记忆、隔离会话。

本包不 import agentscope，也不反向依赖 gateway：领域与存储可以在无框架环境里单测。
"""

from devops_agent.workspace.errors import WorkspaceError
from devops_agent.workspace.models import (
    RepoInfo,
    Repository,
    Workspace,
    WorkspaceRef,
    WorkspaceSession,
)
from devops_agent.workspace.service import WorkspaceService

__all__ = [
    "RepoInfo",
    "Repository",
    "Workspace",
    "WorkspaceError",
    "WorkspaceRef",
    "WorkspaceService",
    "WorkspaceSession",
]
