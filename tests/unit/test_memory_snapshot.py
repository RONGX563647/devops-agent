"""快照可观测性测试（SPEC-0013 §18.5 A19 / A20）。

快照是唯一会「静默丢历史」的地方：请求裁剪有 MEMORY_TRIM，快照裁剪此前
一个事件都不发；版本号也没人写，接口永远报 1。
"""

from __future__ import annotations

import json
from typing import Any

from agentscope.message import UserMsg
from agentscope.state import AgentState

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.memory.snapshot import (
    SNAPSHOT_VERSION_KEY,
    reset_restore_stats,
    restore_stats,
)
from devops_agent.runtime.assembly import _restore_state
from devops_agent.runtime.scope_runtime import AgentScopeRuntime


class _Agent:
    """只带 state 的最小替身：snapshot() 只碰 state 与 memory。"""

    def __init__(self, state: AgentState) -> None:
        self.state = state


def _runtime(tmp_path: Any) -> MemoryRuntime:
    return MemoryRuntime("s1", load_memory(), MemorySink(), tmp_path / "obs.db")


def test_snapshot_writes_version(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """A19：snapshot() 之后 middle_context 里有版本号，/api/context 不再报 1。"""
    runtime = _runtime(tmp_path)
    scope = AgentScopeRuntime(_Agent(AgentState()), memory=runtime)  # type: ignore[arg-type]
    payload = json.loads(scope.snapshot())
    expected = load_memory().context.snapshot_version
    assert payload["middle_context"][SNAPSHOT_VERSION_KEY] == expected
    assert expected >= 2


def test_snapshot_trim_emits_frame(tmp_path: Any) -> None:  # type: ignore[no-untyped-def]
    """A20：快照裁剪丢掉轮次时发 MEMORY_TRIM(reason=snapshot_cap)。"""
    runtime = _runtime(tmp_path)
    runtime.policy = runtime.policy.model_copy(
        update={"context": runtime.policy.context.model_copy(update={"snapshot_max_chars": 200})}
    )
    state = AgentState()
    state.context = [UserMsg(name="s1", content="x" * 500) for _ in range(4)]
    report = runtime.cap_snapshot(_Agent(state))  # type: ignore[arg-type]
    assert report.dropped_turns > 0

    trims = [f for f in runtime.sink.drain() if f["type"] == events.MEMORY_TRIM]
    assert trims and trims[0]["reason"] == "snapshot_cap"
    assert trims[0]["memory"]["dropped_turns"] > 0


def test_corrupt_snapshot_is_counted_not_raised() -> None:
    """A19：坏快照退回新会话，但既不抛异常也不静默 —— 计数 +1、日志带类名。"""
    reset_restore_stats()
    assert _restore_state("s1", "{not json") is None
    stats = restore_stats()
    assert stats["restore_failures"] == 1
    assert "DecodingError" in stats["last_error"] or "Error" in stats["last_error"]
    # 空快照是新会话，不是失败：不能把「第一次进来」算成恢复失败
    assert _restore_state("s2", "") is None
    assert restore_stats()["restore_failures"] == 1
