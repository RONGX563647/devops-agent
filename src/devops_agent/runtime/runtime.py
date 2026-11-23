"""编排层对外唯一入口（协议）。

业务代码只依赖这个协议，不直接 import agentscope。
框架实现放在 agent.py / supervisor.py 内，本模块保持框架无关。

运行时为异步：真实 AgentScope Agent.reply() 是 coroutine，网关必须 await。
"""

from __future__ import annotations

from typing import Protocol


class AgentRuntime(Protocol):
    """Agent 运行时协议（异步）。"""

    async def run(self, session_id: str, user_input: str) -> str:
        """执行一次 Agent 调用，返回结果文本。"""
        ...


class EchoRuntime:
    """框架无关的占位运行时，用于网关自检与集成测试。

    真实 Agent 运行时由 supervisor 装配后注入；此类不 import agentscope。
    """

    async def run(self, session_id: str, user_input: str) -> str:
        return f"[{session_id}] {user_input}"
