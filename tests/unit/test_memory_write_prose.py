"""长期写入只存「说过的话」，不存工具调用的机器负载。

线上库实测的病：长期层里躺着 `{"head_limit": 40, "pattern": "src/.../**/*.py"}`。
根因不是候选过滤漏了，而是取正文时用了 `message_text` —— 它为 token 估算服务，
会把 `tool_call.input` 与 `tool_result.output` 一起拍平；这些行没有句读、却因为
带路径/命令在抽取式摘要里拿到最高优先级，最后整条记忆只剩工具参数。

这一组钉三件事：正文提取只取正文块、写入不落机器负载、只有问题没有回答时不写。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock, UserMsg

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.candidate import is_candidate
from devops_agent.memory.observer import MemoryMiddleware
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.memory.tokens import message_prose, message_text
from devops_agent.observability import repository as repo

CONCLUSION = "根因是摘要把带路径的 JSON 行当成高优先级句子，因此写入的正文全是工具参数。"


def _runtime(tmp_path: Path) -> MemoryRuntime:
    return MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    with repo.connect(tmp_path / "obs.db") as conn:
        repo.init_db(conn)
        return repo.list_long_term(conn, scope="session", scope_id="session:s1")


def _tool_call_msg() -> Msg:
    return Msg(
        name="devops",
        role="assistant",
        content=[
            TextBlock(type="text", text=CONCLUSION),
            ToolCallBlock(
                type="tool_call",
                id="c1",
                name="Grep",
                input='{"pattern": "src/devops_agent/memory/**/*.py", "head_limit": 40}',
            ),
            ToolResultBlock(type="tool_result", id="c1", name="Grep", output="runtime.py:1"),
        ],
    )


def _write(agent: Msg, user_text: str, tmp_path: Path) -> list[dict[str, Any]]:
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)

    class _Agent:
        class state:  # noqa: N801 - 只需要 context 一个属性
            context = [agent]

    async def source() -> AsyncGenerator[dict[str, Any], None]:
        yield {"type": "REPLY_END"}

    async def collect() -> None:
        async for _ in middleware.on_reply(
            _Agent(), {"inputs": UserMsg(name="s1", content=user_text)}, source
        ):
            pass

    asyncio.run(collect())
    return _rows(tmp_path)


def test_message_prose_drops_tool_payload() -> None:
    """正文提取只留 TextBlock：工具入参与工具结果都不算「说过的话」。"""
    msg = _tool_call_msg()
    assert message_prose(msg) == CONCLUSION
    # 对照组：token 估算那条路径仍然要看到全部内容，否则计数会偏低
    flattened = message_text(msg)
    assert "head_limit" in flattened and "runtime.py:1" in flattened


def test_write_keeps_prose_not_tool_arguments(tmp_path: Path) -> None:
    """写入的正文是结论，不是工具参数 JSON。"""
    rows = _write(_tool_call_msg(), "为啥工具调用失败这么多？", tmp_path)
    assert len(rows) == 1
    text = str(rows[0]["text"])
    assert "head_limit" not in text
    assert "根因" in text


def test_tool_only_reply_does_not_store_the_question(tmp_path: Path) -> None:
    """回答正文为空（只有工具调用）且用户只是在提问：不写长期层。

    tool-only 回合以前会把问句本身当记忆存下来 —— 下次召回回来的就是「用户问过
    什么」，没有任何信息量，还占掉 note 预算。
    """
    tool_only = Msg(
        name="devops",
        role="assistant",
        content=[
            ToolCallBlock(
                type="tool_call",
                id="c1",
                name="Glob",
                input='{"pattern": "**/*.py", "head_limit": 5}',
            )
        ],
    )
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)

    class _Agent:
        class state:  # noqa: N801
            context = [tool_only]

    async def source() -> AsyncGenerator[dict[str, Any], None]:
        yield {"type": "REPLY_END"}

    async def collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in middleware.on_reply(
                _Agent(),
                {"inputs": UserMsg(name="s1", content="最近失败最多的阶段是哪个？")},
                source,
            )
        ]

    asyncio.run(collect())
    assert _rows(tmp_path) == []
    skipped = [f for f in runtime.sink.drain() if f["type"] == events.MEMORY_WRITE_END]
    assert skipped and skipped[0]["reason"] == "question_only"


def test_question_without_answer_is_not_a_candidate() -> None:
    ok, reason = is_candidate("这个仓库的 CI 是怎么跑的？", "")
    assert ok is False
    assert reason == "question_only"


def test_statement_without_answer_is_still_a_candidate() -> None:
    """用户陈述事实、助手没说话：照常可写 —— 别把「没有回答」一刀切成不记。"""
    ok, _reason = is_candidate("以后统一用 ruff，不要用 flake8", "")
    assert ok is True
