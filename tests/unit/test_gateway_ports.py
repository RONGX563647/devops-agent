"""两个端口的行为测试：Null 能力与挂起态读写。

重点不是「方法能跑」，而是**缺能力时的语义**：Echo / 只实现 stream 的替身应该
给出空列表、None、空 dict，而不是 AttributeError 或静默空流。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from devops_agent.gateway.contract import PendingPayload
from devops_agent.gateway.ports import (
    PendingRun,
    PendingStore,
    RuntimeCapabilityError,
    RuntimePort,
)
from devops_agent.observability.recorder import RunRecorder


class _StreamOnly:
    """只实现协议强制项 stream 的替身（等价于最小可用运行时）。"""

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "REPLY_END", "finished_reason": "completed"}


class _Full:
    """九项能力全齐的替身：对应 AgentScopeRuntime 的公开面。"""

    web_search = True

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.resumed = 0

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "REPLY_END", "finished_reason": "completed"}

    def pending_calls(self) -> list[dict[str, Any]]:
        return self.pending

    def snapshot(self) -> str:
        return '{"context": []}'

    def model_params_record(self) -> dict[str, Any]:
        return {"temperature": 0.2}

    async def resume(self, decisions: list[Any]) -> AsyncIterator[dict[str, Any]]:
        self.resumed += 1
        yield {"type": "REPLY_END", "finished_reason": "completed"}


def test_stream_only_runtime_gets_null_capabilities() -> None:
    port = RuntimePort(_StreamOnly())
    assert port.resumable is False
    assert port.pending() == [], "没有挂起能力 == 没有挂起调用"
    assert port.snapshot() is None, "None 表示「不支持」，不是「空快照」"
    assert port.params_record() == {}
    assert port.web_search() is None


def test_resume_without_capability_raises_instead_of_empty_stream() -> None:
    """静默返回空流会让「续跑没发生」表现成模型答非所问，所以必须显式报错。"""
    with pytest.raises(RuntimeCapabilityError):
        RuntimePort(_StreamOnly()).resume([])


def test_full_runtime_exposes_every_capability() -> None:
    runtime = _Full()
    runtime.pending = [{"tool_call_id": "c1"}]
    port = RuntimePort(runtime)
    assert port.resumable is True
    assert port.pending() == [{"tool_call_id": "c1"}]
    assert port.snapshot() == '{"context": []}'
    assert port.params_record() == {"temperature": 0.2}
    assert port.web_search() is True


def test_pending_run_ownership_and_expiry() -> None:
    row = PendingRun(
        run_id=1,
        session_id="s1",
        parked=True,
        expires_at="2026-01-01T00:00:00+00:00",
        permission="workspace",
        payload=PendingPayload(),
    )
    assert row.owned_by("s1") is True
    assert row.owned_by("s2") is False, "只凭 run_id 读到别人的命令原文就是越权"
    assert row.is_expired("2026-01-02T00:00:00+00:00") is True
    assert row.is_expired("2025-12-31T00:00:00+00:00") is False


def test_pending_run_without_expiry_never_expires() -> None:
    """老记录没有 expires_at：宁可一直可确认，也不要凭空判成过期。"""
    row = PendingRun(1, "s1", True, "", "", PendingPayload())
    assert row.is_expired("2999-01-01T00:00:00+00:00") is False


def test_pending_store_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "pending.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))
    recorder = RunRecorder(db)
    run_id = recorder.start("s1", "删掉 dist")
    payload = PendingPayload(
        tool_calls=[{"tool_call_id": "c1", "input": "rm -rf dist"}],
        expires_at="2999-01-01T00:00:00+00:00",
    )
    assert recorder.park(payload.to_json(), payload.expires_at) is True

    store = PendingStore()
    row = store.load(run_id)
    assert row is not None
    assert row.parked is True
    assert row.tool_calls == [{"tool_call_id": "c1", "input": "rm -rf dist"}]
    assert store.load(run_id + 999) is None

    # 打扰计数只在有 run 时可得；None 是「不可得」这个事实，不是上限
    assert store.bump_asks(None) is None
    assert store.bump_asks(run_id) == 1

    # 解挂幂等：第二次条件更新命中 0 行，调用方据此回 409
    assert store.resolve(run_id, now="2000-01-01T00:00:00+00:00") is True
    assert store.resolve(run_id, now="2000-01-01T00:00:00+00:00") is False
