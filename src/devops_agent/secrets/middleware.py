"""密钥注入中间件 —— 明文只在这一层存在，且只在一次工具调用的生命周期内。

职责边界（为什么放在工具层而不是提示词层）：
- 提示词层做不到「不给模型看」：只要值出现在上下文里，它就会被带进下一轮；
- 工具层是唯一「模型给指令、真值被需要」的交汇点。入参在这里补真值，
  返回在这里抹回占位符，模型看到的始终是 `{{secret:name}}`。

设计细节：
- 递归替换入参里的字符串（命令、URL、请求体都可能是嵌套结构），非字符串原样透传；
- 回抹只处理**本次真正注入过**的密钥（避免误伤偶然相似串、库越大越慢）；
- 但工具**自己从磁盘读到**的明文（`cat config.yaml`、`printenv`）不在注入集合里，
  由 `capture_output` 兜底：识别后**登记进保险箱**并换成占位符，人可在密钥页查看；
- 解不出的占位符原样保留，让工具自己报「没有这个变量」，而不是让中间件炸掉。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolChunk, ToolMiddlewareBase

from devops_agent.redaction import rules
from devops_agent.secrets.capture import Captured, capture_output
from devops_agent.secrets.vault import inject, mask, resolver

# 「名字 → 明文」的解析函数；解不出返回 None
Resolver = Callable[[str], str | None]
# 输出捕获的登记函数：接收一条捕获项，返回最终入库名；保箱不可用时可降级
Capture = Callable[[Captured], str]

# 读源码的工具：输出走 `profile="code"`，关掉熵兜底 —— 否则函数名、模块路径
# 会被当成高熵凭据，模型读到的源码不可编译（SPEC-0015 §3）。
# 这里用工具名做映射而不是让工具自报：MCP 工具接入时再改为声明式（见规格 §11）。
CODE_PROFILE_TOOLS = frozenset({"Read", "Grep", "Glob"})

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


def _make_capture(capture: Capture | None) -> Capture:
    """默认登记函数：复用已入库同值的名字，否则写入 source=output。

    保箱不可用时退化为「用确定性名字、不入库」——明文仍被换成占位符，
    模型上下文先保住，人查看时再看不到的代价由降级提示承担，不让工具调用失败。
    """
    if capture is not None:
        return capture

    def _register(item: Captured) -> str:
        try:
            from devops_agent.secrets.vault import get_vault

            vault = get_vault()
            existing = vault.name_for_value(item.value)
            if existing:
                return existing
            vault.put(item.name, item.value, source="output")
        except Exception:  # 保箱损坏：明文仍换占位符，只是不入库
            logger.warning("输出捕获登记失败：凭据 %s 未入库", item.name)
        return item.name

    return _register


def _profile_for(tool: Any) -> str:
    """按工具名选脱敏档案：读源码的工具不做熵兜底，其余按输出处理。"""
    name = str(getattr(tool, "name", ""))
    return rules.PROFILE_CODE if name in CODE_PROFILE_TOOLS else rules.PROFILE_OUTPUT


def _mask_chunk(
    chunk: ToolChunk, resolved: Mapping[str, str], capture: Capture, profile: str
) -> ToolChunk:
    """回抹本次注入的真值 + 捕获输出里新出现的明文；非文本块递归处理 text 字段。

    非文本块（结构化/附件）也常带 `text` 字段，不能默默透传——递归处理
    一层已知字段，其余原样保留，避免破坏框架自有块类型。
    """
    content: list[Any] = []
    changed = False
    for block in chunk.content:
        if isinstance(block, TextBlock):
            text = mask(block.text, dict(resolved))
            text, captured = capture_output(text, register=capture, profile=profile)
            if text != block.text:
                changed = True
            content.append(block.model_copy(update={"text": text}))
            if captured:
                logger.debug("输出捕获 %d 条凭据：%s", len(captured), [c.name for c in captured])
        else:
            # 结构化块里的 text 字段递归处理；无该字段则原样透传
            raw = getattr(block, "text", None)
            if isinstance(raw, str):
                text = mask(raw, dict(resolved))
                text, _ = capture_output(text, register=capture, profile=profile)
                if text != raw:
                    changed = True
                    block = block.model_copy(update={"text": text})
            content.append(block)
    if not changed:
        return chunk
    return chunk.model_copy(update={"content": content})


class SecretInjectionMiddleware(ToolMiddlewareBase):
    """工具调用边界上的密钥注入 / 回抹。"""

    def __init__(self, resolve: Resolver | None = None, capture: Capture | None = None) -> None:
        self._resolve = resolve if resolve is not None else resolver()
        self._capture = _make_capture(capture)

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
        profile = _profile_for(tool)
        async for chunk in next_handler(**replaced):
            # 无条件过一遍：即便本次没注入任何密钥，工具也可能自己读到明文。
            yield _mask_chunk(chunk, resolved, self._capture, profile)


def build_secret_middleware(
    resolve: Resolver | None = None, capture: Capture | None = None
) -> SecretInjectionMiddleware:
    """装配入口：默认用进程级保险箱，测试可注入自己的解析/登记函数。"""
    return SecretInjectionMiddleware(resolve, capture)
