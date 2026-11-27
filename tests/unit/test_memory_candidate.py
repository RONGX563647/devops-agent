"""长期写入候选过滤测试（SPEC-0013 §18.5 A23）。

长期层放的是「稳定事实」；寒暄与纯确认进去一次，就会在之后每一轮被召回。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.candidate import is_candidate
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink


@pytest.mark.parametrize(
    "user",
    ["你好", "在吗？", "谢谢！", "好的", "嗯嗯", "继续", "ok", "收到，辛苦了"],
)
def test_courtesy_turns_are_not_candidates(user: str) -> None:
    ok, reason = is_candidate(user, "有什么可以帮你的？")
    assert ok is False
    assert reason == "courtesy"


def test_empty_turn_is_not_candidate() -> None:
    assert is_candidate("", "") == (False, "empty")


def test_short_turn_without_signal_is_rejected() -> None:
    ok, reason = is_candidate("帮我看看", "好")
    assert ok is False and reason == "too_short"


def test_short_turn_with_path_or_command_is_kept() -> None:
    """带路径/命令的短回合信息量高：长度下限对它放宽。"""
    ok, _ = is_candidate("跑 pytest -q", "通过")
    assert ok is True
    ok, _ = is_candidate("看 src/app.py", "改好了")
    assert ok is True


def test_substantive_turn_is_kept() -> None:
    ok, _ = is_candidate(
        "这个仓库的 CI 用 ruff 和 mypy，提交前必须跑一遍", "记住了，会在提交前跑。"
    )
    assert ok is True


def test_write_turn_skips_courtesy_and_emits_reason(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    runtime = MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")
    result = asyncio.run(runtime.write_turn("谢谢你", "不客气"))
    assert result["stored"] is False and result["reason"] == "courtesy"
    writes = [f for f in runtime.sink.drain() if f["type"] == events.MEMORY_WRITE_END]
    assert writes and writes[0]["state"] == "skipped" and writes[0]["reason"] == "courtesy"


def test_write_turn_stores_substantive_turn(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    runtime = MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")
    result = asyncio.run(
        runtime.write_turn("部署脚本在 scripts/deploy.sh，必须先跑 ruff", "记下了。")
    )
    assert result["stored"] is True


def test_meta_preamble_is_not_candidate() -> None:
    """「我先看一下……」说的是下一步要做什么，不是结论（SPEC-0019 §7.3）。"""
    ok, reason = is_candidate("以上问题也修复了", "我先看一下工作区里实际的东西")
    assert ok is False
    assert reason == "meta_preamble"


def test_meta_opener_with_technical_signal_is_still_stored() -> None:
    ok, _ = is_candidate("看看这个文件", "我先读一下 src/devops_agent/memory/runtime.py")
    assert ok is True
