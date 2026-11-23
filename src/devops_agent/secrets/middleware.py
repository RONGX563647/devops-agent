"""密钥注入中间件 —— 明文只在这一层存在，且只在一次工具调用的生命周期内。

职责边界（为什么放在工具层而不是提示词层）：
- 提示词层做不到「不给模型看」：只要值出现在上下文里，它就会被带进下一轮；
- 工具层是唯一「模型给指令、真值被需要」的交汇点。入参在这里补真值，
  返回在这里抹回占位符，模型看到的始终是 `{{secret:name}}`。

设计细节：
- 递归替换入参里的字符串（命令、URL、请求体都可能是嵌套结构），非字符串原样透传；
- 只抹回**本次真正注入过**的密钥，不做全库扫描：否则工具输出里偶然出现的
  相似串会被误伤，且库越大越慢；
- 解不出的占位符原样保留，让工具自己报「没有这个变量」，而不是让中间件炸掉。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolChunk, ToolMiddlewareBase

from devops_agent.secrets.vault import inject, mask, resolver

# 「名字 → 明文」的解析函数；解不出返回 None
Resolver = Callable[[str], str | None]

# 只在 debug 级别记一行「哪个工具用掉了几个占位符」，便于排查注入没生效的场景
logger = logging.getLogger(__name__)


def _tracking(resolve: Resolver, resolved: dict[str, str]) -> Resolver:
    """包一层解析函数：顺带记下本次实际注入的「名字 → 真值」。"""

    def _resolve(name: str) -> str | None:
        secret = resolve(name)
        if secret is not None:
            resolved[name] = secret
        return secret

    return _resolve


def _inject_value(value: Any, resolve: Resolver) -> Any:
    """深度替换一个入参值：字符串注真值，容器逐项递归，其余原样。"""
    if isinstance(value, str):
        return inject(value, resolve)[0]
    if isinstance(value, dict):
        return {key: _inject_value(item, resolve) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_inject_value(item, resolve) for item in value]
    return value


def _mask_chunk(chunk: ToolChunk, resolved: Mapping[str, str]) -> ToolChunk:
    """把一个返回块里的真值抹回占位符；非文本块（图片等）原样透传。"""
    content: list[Any] = []
    for block in chunk.content:
        if isinstance(block, TextBlock):
            content.append(block.model_copy(update={"text": mask(block.text, dict(resolved))}))
        else:
            content.append(block)
    return chunk.model_copy(update={"content": content})


class SecretInjectionMiddleware(ToolMiddlewareBase):
    """工具调用边界上的密钥注入 / 回抹。"""

    def __init__(self, resolve: Resolver | None = None) -> None:
        self._resolve = resolve if resolve is not None else resolver()

    async def on_tool_call(  # type: ignore[override]
        self,
        tool: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        """先补真值再执行，执行结果逐块抹回占位符。

        标注 ignore[override] 的原因：框架基类把返回类型写成
        `AsyncGenerator[...]` 却没在该方法里 yield，mypy 因此把它算成
        「返回协程」，与本实现（异步生成器）判为不兼容。而框架调用点
        `async for chunk in mw.on_tool_call(...)` 需要的正是异步生成器，
        基类注释里的示例也是 `yield` 写法——这里按真实调用约定实现。
        """
        resolved: dict[str, str] = {}
        replaced = _inject_value(input_kwargs, _tracking(self._resolve, resolved))
        if not isinstance(replaced, dict):  # 顶层入参必然是 dict，兜底防崩
            replaced = input_kwargs
        if resolved:
            logger.debug("向工具 %s 注入 %d 个密钥", getattr(tool, "name", "?"), len(resolved))
        async for chunk in next_handler(**replaced):
            yield _mask_chunk(chunk, resolved) if resolved else chunk


def build_secret_middleware(resolve: Resolver | None = None) -> SecretInjectionMiddleware:
    """装配入口：默认用进程级保险箱，测试可注入自己的解析函数。"""
    return SecretInjectionMiddleware(resolve)
