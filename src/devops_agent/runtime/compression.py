"""上下文压缩的可观测性 —— 把 agentscope 的压缩过程变成流式事件。

为什么需要单独一层：agentscope 在每轮推理前自动调用 `compress_context()`，
但它的 `on_compress_context` 中间件钩子**没有事件通道**（返回 None），
压缩调用本身也不发 `MODEL_CALL_END`（框架在 `_agent.py` 里明确注释过）。

于是链路是：

    on_compress_context 钩子 → 进程内的 CompressionSink 队列
        → AgentScopeRuntime.stream() drain → SSE 帧 → 前端

这样不用改 agentscope 源码，也不用往 AgentState 里塞 UI 专用的东西。

三个事件：

- `CONTEXT_BUDGET`：流开始时给一次阈值（默认 0.8 × 128000 = 102400）；
- `CONTEXT_COMPRESSION_START`：触发检查，带检查前的 token 数；
- `CONTEXT_COMPRESSION_END`：检查结束，带前后 token、是否真的压缩、摘要长度。

`START` 在没达到阈值时也会发，`END.compressed` 为 False 表示「检查过但没压」。
前端只把真正压缩的记录画进时间线，避免每次推理都刷一条无效记录。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.agent import Agent, ContextConfig
from agentscope.middleware import MiddlewareBase

from devops_agent.config import load_model

CONTEXT_BUDGET = "CONTEXT_BUDGET"
CONTEXT_COMPRESSION_START = "CONTEXT_COMPRESSION_START"
CONTEXT_COMPRESSION_END = "CONTEXT_COMPRESSION_END"


def _fallback() -> tuple[float, int]:
    """兜底值直接来自 config/model.yaml —— 这里不留第二份常量。

    阈值 = trigger_ratio × context_size，agentscope 内部也是这么算的
    （`_agent.py` 的 `_compress_context_impl`），这里只复述，不另立口径。
    """
    settings = load_model()
    return settings.trigger_ratio, settings.context_size


def context_budget(agent: Agent) -> dict[str, Any]:
    """给前端的上下文预算：阈值、模型窗口、触发比例。"""
    ratio_fallback, size_fallback = _fallback()
    config: ContextConfig = getattr(agent, "context_config", None) or ContextConfig()
    ratio = float(getattr(config, "trigger_ratio", ratio_fallback))
    model = getattr(agent, "model", None)
    size = int(getattr(model, "context_size", size_fallback))
    return {
        "type": CONTEXT_BUDGET,
        "threshold": int(ratio * size),
        "context_size": size,
        "trigger_ratio": ratio,
    }


async def count_context_tokens(agent: Agent) -> int:
    """按 agentscope 自己的口径估算当前上下文 token 数。

    `_prepare_model_input()` 是私有方法，但 agentscope 自己计数时用的就是它
    （_agent.py 的 _compress_context_impl 与上下文长度提示都这么调）。
    取不到时返回 0 —— 可观测性链路永远不能让主流程失败。
    """
    try:
        kwargs = await agent._prepare_model_input()  # noqa: SLF001 - 与框架计数口径一致
        return int(await agent.model.count_tokens(**kwargs))
    except Exception:  # noqa: BLE001 - 计数失败不该中断一次正常压缩
        return 0


class CompressionSink:
    """运行时持有的帧队列：钩子写，流读取。

    用 deque 而不是 asyncio.Queue：写入是同步的（钩子在框架的事件循环里），
    读取也是同步的（在 `async for` 的两次 yield 之间），不需要跨任务同步。
    """

    def __init__(self) -> None:
        self._frames: deque[dict[str, Any]] = deque()

    def emit(self, frame: dict[str, Any]) -> None:
        """钩子侧写入一帧。"""
        self._frames.append(frame)

    def drain(self) -> list[dict[str, Any]]:
        """取走当前所有待发帧，保持框架事件的原有顺序。"""
        frames = list(self._frames)
        self._frames.clear()
        return frames


class CompressionMiddleware(MiddlewareBase):
    """挂在 Agent 的 middleware 链上，记录每次 compress_context 检查。"""

    def __init__(self, sink: CompressionSink) -> None:
        self._sink = sink

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        """洋葱钩子：先记检查前状态，再交给框架执行，最后记结果。"""
        ratio_fallback, size_fallback = _fallback()
        config = input_kwargs.get("context_config") or getattr(agent, "context_config", None)
        ratio = float(
            getattr(config, "trigger_ratio", ratio_fallback)
            if config is not None
            else ratio_fallback
        )
        size = int(getattr(agent.model, "context_size", size_fallback))
        threshold = int(ratio * size)
        before = await count_context_tokens(agent)
        summary_before = getattr(agent.state, "summary", "") or ""
        context_before = len(agent.state.context)
        self._sink.emit(
            {
                "type": CONTEXT_COMPRESSION_START,
                "before_tokens": before,
                "threshold": threshold,
            }
        )
        await next_handler()
        after = await count_context_tokens(agent)
        self._sink.emit(
            {
                "type": CONTEXT_COMPRESSION_END,
                "before_tokens": before,
                "after_tokens": after,
                "threshold": threshold,
                "compressed": (
                    (getattr(agent.state, "summary", "") or "") != summary_before
                    or len(agent.state.context) != context_before
                ),
                "summary_chars": len(getattr(agent.state, "summary", "") or ""),
            }
        )
