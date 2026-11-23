"""编排层对外唯一入口（协议）。

业务代码只依赖这个协议，不直接 import agentscope。
框架实现放在 agent.py / supervisor.py 内，本模块保持框架无关。

运行时为异步：真实 AgentScope Agent.reply() 是 coroutine，网关必须 await。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class AgentRuntime(Protocol):
    """Agent 运行时协议（异步）。"""

    def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """流式执行，逐个吐出可观测事件 dict（普通数据，框架无关）。"""
        ...


class EchoRuntime:
    """框架无关的占位运行时，用于网关自检与集成测试。

    真实 Agent 运行时由 supervisor 装配后注入；此类不 import agentscope。
    """

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        """占位流式实现：吐出最小事件序列，供网关与前端自检。"""
        yield {"type": "REPLY_START", "session_id": session_id}
        yield {"type": "TEXT_BLOCK_DELTA", "delta": f"[{session_id}] {user_input}"}
        yield {"type": "REPLY_END", "finished_reason": "completed"}
