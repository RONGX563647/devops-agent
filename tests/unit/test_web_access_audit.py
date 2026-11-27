"""网络获取决策的审计（SPEC-0012 §7）。

要回答的问题只有一句：**这一轮到底联网了没有、为什么没抓、缺了什么**。
所以断言落在 `runs.web_access` 的 JSON 形状上，而不是日志。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent.gateway.app import app
from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import RunRecorder

client = TestClient(app)


def test_recorder_persists_web_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多次 note 合并成一份 JSON；空决策不写库（老记录保持空串 = 未记录）。"""
    db = tmp_path / "audit.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))
    recorder = RunRecorder(db)
    run_id = recorder.start("s1", "hi")
    recorder.note_web_access(net=True)
    recorder.note_web_access(fallback="1 条引用内容不足", prefetch="1 条内容不足")
    recorder.finish()

    with repo.connect(db) as conn:
        run = repo.get_run(conn, run_id)
    assert run is not None
    assert json.loads(run.web_access) == {
        "net": True,
        "fallback": "1 条引用内容不足",
        "prefetch": "1 条内容不足",
    }


def test_run_without_decision_keeps_empty_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有决策时留空串：读取方要能区分「未记录」与「没联网」。"""
    db = tmp_path / "audit-empty.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))
    recorder = RunRecorder(db)
    run_id = recorder.start("s1", "hi")
    recorder.finish()

    with repo.connect(db) as conn:
        run = repo.get_run(conn, run_id)
    assert run is not None
    assert run.web_access == ""


def test_stream_records_net_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """走一遍真实端点：Echo 没有检索能力，`net` 必须如实记成 null 而不是 true。"""
    db = tmp_path / "audit-stream.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))
    original = app.state.runtime
    runtime = _EchoWithPending()
    app.state.runtime = runtime
    try:
        with client.stream(
            "POST",
            "/run/stream",
            json={"session_id": "s1", "user_input": "看 https://example.com/x"},
        ) as resp:
            body = "".join(resp.iter_text())
    finally:
        app.state.runtime = original
        app.state.sessions = None
        app.state.sessions_base = None

    assert "RUN_DONE" in body or "RUN_PARKED" in body
    with repo.connect(db) as conn:
        runs = repo.list_runs(conn, limit=1)
    assert runs, body
    data = json.loads(runs[0].web_access)
    assert data["net"] is None
    # 远程抓取默认关闭：预抓取阶段会说「未开启远程抓取」，并带上 fallback
    assert "expand" not in data or data["fallback"] == ""


class _EchoWithPending:
    """最小替身：只实现 stream，且没有 web_search 能力（等价于 Echo）。"""

    async def stream(self, session_id: str, user_input: str):  # noqa: ANN201
        yield {"type": "REPLY_START", "session_id": session_id}
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "ok"}
        yield {"type": "REPLY_END", "finished_reason": "completed"}
