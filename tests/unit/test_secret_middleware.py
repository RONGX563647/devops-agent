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
KEY_FROM_DISK = "AKIA" + "Z" * 16  # 模拟工具从 config.yaml 读到的明文，非注入


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


def test_captures_tool_read_secret_even_without_injection() -> None:
    """工具自己从磁盘读到的明文（不在注入集合里）也必须被捕获。

    这是原来 `yield chunk if resolved else chunk` 的漏洞：resolved 为空时
    连 mask 都不走，cat/printenv 读到的 key 原样进上下文与 spans。
    """
    from devops_agent.secrets.capture import Captured

    registered: list[Captured] = []

    def capture(item: Captured) -> str:
        registered.append(item)
        return item.name

    middleware = build_secret_middleware(_resolver({}), capture=capture)

    async def handler(**_: Any) -> Any:
        yield ToolChunk(
            content=[TextBlock(type="text", text="config: AWS_KEY=" + KEY_FROM_DISK)],
            state=ToolResultState.SUCCESS,
        )

    async def collect() -> list[ToolChunk]:
        return [
            chunk
            async for chunk in middleware.on_tool_call(
                tool=None, input_kwargs={"command": "cat config.yaml"}, next_handler=handler
            )
        ]

    text = asyncio.run(collect())[0].content[0].text
    assert KEY_FROM_DISK not in text
    assert "{{secret:" in text
    assert len(registered) == 1
    assert registered[0].value == KEY_FROM_DISK


def test_capture_does_not_rebuild_chunk_when_clean() -> None:
    """输出没有任何敏感串时不重建块，保持原样返回。"""
    from devops_agent.secrets.capture import Captured

    def capture(item: Captured) -> str:
        return item.name

    middleware = build_secret_middleware(_resolver({}), capture=capture)
    chunk = _chunk("all clear, nothing secret here")

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


def test_read_tool_source_code_is_byte_identical() -> None:
    """读源码走代码档案：`def estimate_prefix_tokens` 不许被打成占位符。

    旧实现把它当高熵凭据，模型拿到的 `runtime.py` 不可编译——这不是可读性问题，
    而是「改代码的模型拿到了坏输入」（SPEC-0015 §3）。
    """
    from devops_agent.secrets.capture import Captured

    source = (
        "def estimate_prefix_tokens(system_text: str) -> int:\n"
        "    from devops_agent.memory.tokens import estimate_tokens\n"
        "    return estimate_tokens(system_text)\n"
    )

    def capture(item: Captured) -> str:
        return item.name

    middleware = build_secret_middleware(_resolver({}), capture=capture)

    class ReadTool:
        name = "Read"

    async def handler(**_: Any) -> Any:
        yield _chunk(source)

    async def collect() -> list[ToolChunk]:
        return [
            chunk
            async for chunk in middleware.on_tool_call(
                tool=ReadTool(), input_kwargs={"file_path": "runtime.py"}, next_handler=handler
            )
        ]

    assert asyncio.run(collect())[0].content[0].text == source


def test_readonly_shell_command_uses_code_profile() -> None:
    """`cat` / `sed -n` 读源码与 Read 同一风险面；写盘与 printenv 不降档。

    这条钉的是档案选择，不是脱敏结果：两个档案的差异只在熵兜底，
    构造一个「只在 output 命中」的样本等于造一个真实中不存在的形状。
    """
    from devops_agent.secrets.middleware import _profile_for

    bash = type("Bash", (), {"name": "Bash"})()
    read = type("Read", (), {"name": "Read"})()
    assert _profile_for(bash, {"command": "sed -n '1,80p' src/a.py"}) == "code"
    assert _profile_for(bash, {"command": "cat src/a.py | grep token"}) == "code"
    assert _profile_for(bash, {"command": "printenv"}) == "output"
    assert _profile_for(bash, {"command": "cat a.py > out.txt"}) == "output"
    assert _profile_for(bash, {"command": "sed -i 's/a/b/' a.py"}) == "output"
    assert _profile_for(read, {}) == "code"
