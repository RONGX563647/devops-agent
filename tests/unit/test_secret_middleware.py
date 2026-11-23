"""密钥注入中间件：入参补真值、输出抹回占位符。

这一层是「模型看不到明文」的唯一保证点，所以两条路径都要钉住：
注入必须穿透嵌套结构，回抹必须覆盖工具原样回显的场景。

（仓库未装异步测试插件，所以统一用 asyncio.run 驱动，与既有 async 代码测试一致。）
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agentscope.message import TextBlock
from agentscope.tool import ToolChunk
from agentscope.tool._response import ToolResultState

from devops_agent.secrets.middleware import SecretInjectionMiddleware, build_secret_middleware

SECRET = "sk-live-0123456789abcdef"


def _chunk(text: str) -> ToolChunk:
    return ToolChunk(content=[TextBlock(type="text", text=text)], state=ToolResultState.SUCCESS)


async def _run(
    middleware: SecretInjectionMiddleware, kwargs: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[ToolChunk]]:
    """跑一次中间件，返回 (工具实际收到的入参, 模型实际收到的块)。"""
    seen: list[dict[str, Any]] = []

    async def handler(**inner: Any) -> Any:
        seen.append(inner)
        yield _chunk(f"out: {inner}")

    chunks = [
        chunk
        async for chunk in middleware.on_tool_call(
            tool=None, input_kwargs=kwargs, next_handler=handler
        )
    ]
    return seen, chunks


def _drive(
    middleware: SecretInjectionMiddleware, kwargs: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[ToolChunk]]:
    return asyncio.run(_run(middleware, kwargs))


def _resolver(mapping: dict[str, str]) -> Any:
    return mapping.get


def test_injects_into_flat_string() -> None:
    middleware = build_secret_middleware(_resolver({"token": SECRET}))
    seen, chunks = _drive(middleware, {"command": "curl -H {{secret:token}} /x"})
    assert SECRET in seen[0]["command"]
    # 工具把入参回显了：模型收到的仍是占位符
    text = chunks[0].content[0].text
    assert SECRET not in text
    assert "{{secret:token}}" in text


def test_injects_into_nested_structures() -> None:
    """请求体、headers 常是嵌套结构：只替换顶层字符串会漏掉大半场景。"""
    middleware = build_secret_middleware(_resolver({"token": SECRET}))
    seen, _ = _drive(
        middleware,
        {
            "url": "https://api.example.com",
            "headers": {"Authorization": "Bearer {{secret:token}}"},
            "items": ["{{secret:token}}", 1, None],
        },
    )
    assert seen[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert seen[0]["items"] == [SECRET, 1, None]
    assert seen[0]["url"] == "https://api.example.com"


def test_unknown_placeholder_passes_through() -> None:
    """解不出的占位符原样留给工具，由工具自己报错，而不是中间件炸掉。"""
    middleware = build_secret_middleware(_resolver({}))
    seen, _ = _drive(middleware, {"command": "echo {{secret:missing}}"})
    assert seen[0]["command"] == "echo {{secret:missing}}"


def test_masks_only_values_actually_injected() -> None:
    """没被注入的值不做回抹：否则工具输出里偶然的同形串会被误伤。"""
    middleware = build_secret_middleware(_resolver({"token": SECRET}))
    _, chunks = _drive(middleware, {"command": "echo {{secret:other}}"})
    assert "{{secret:other}}" in chunks[0].content[0].text


def test_output_is_masked_even_when_secret_came_from_args() -> None:
    middleware = build_secret_middleware(_resolver({"token": SECRET}))

    async def handler(**_: Any) -> Any:
        yield ToolChunk(
            content=[TextBlock(type="text", text=SECRET)], state=ToolResultState.SUCCESS
        )

    async def collect() -> list[ToolChunk]:
        return [
            chunk
            async for chunk in middleware.on_tool_call(
                tool=None, input_kwargs={"command": "{{secret:token}}"}, next_handler=handler
            )
        ]

    assert asyncio.run(collect())[0].content[0].text == "{{secret:token}}"


def test_passthrough_when_nothing_to_inject() -> None:
    """无占位符时不重建块对象，避免无谓开销与意外改动。"""
    middleware = build_secret_middleware(_resolver({"token": SECRET}))
    chunk = _chunk("plain")

    async def handler(**_: Any) -> Any:
        yield chunk

    async def collect() -> list[ToolChunk]:
        return [
            item
            async for item in middleware.on_tool_call(
                tool=None, input_kwargs={"command": "ls"}, next_handler=handler
            )
        ]

    assert asyncio.run(collect())[0] is chunk


def test_middleware_default_resolver_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认解析函数在保箱不可用时返回 None，构造中间件本身不该失败。"""
    from devops_agent.secrets import vault as vault_mod

    def boom() -> object:
        raise RuntimeError("no vault")

    monkeypatch.setattr(vault_mod, "get_vault", boom)
    assert SecretInjectionMiddleware() is not None
