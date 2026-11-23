"""记忆 —— 用内置长期记忆中间件，替代手搓 append-only 台账。

Mem0Middleware / AgenticMemoryMiddleware / ReMeMiddleware 提供长期记忆，
任务级状态用 agentscope.state.AgentState。
"""

from __future__ import annotations

from typing import Any

from agentscope.middleware import Mem0Middleware


def build_mem0_middleware(
    user_id: str,
    *,
    client: Any = None,
    mem0_config: dict[str, Any] | None = None,
    mode: str = "both",
    top_k: int = 5,
) -> Mem0Middleware:
    """装配 Mem0 长期记忆中间件。

    需提供 client 或 mem0_config 之一（Mem0Middleware 的构造约束）。
    """
    return Mem0Middleware(
        user_id=user_id,
        client=client,
        mem0_config=mem0_config,
        mode=mode,  # type: ignore[arg-type]
        top_k=top_k,
    )
