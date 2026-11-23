"""P1 边界分支单测：异常路径、类型转换、真实流式适配。"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from devops_agent.main import _sse_frame, app
from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import RunRecorder, _as_int
from devops_agent.runtime.agent import AgentScopeRuntime
from devops_agent.runtime.events import event_to_dict

client = TestClient(app)


@dataclass
class FakeError:
    type: object

    def __str__(self) -> str:
        return "boom"


class FakeValue:
    def __init__(self, value: str) -> None:
        self.value = value


def test_as_int_branches() -> None:
    assert _as_int(42) == 42
    assert _as_int("17") == 17
    assert _as_int("not-int") == 0
    assert _as_int(None) == 0


def test_event_to_dict_with_error() -> None:
    @dataclass
    class E:
        type: object
        error: object

    data = event_to_dict(E(type=FakeValue("REPLY_END"), error=FakeError("ModelError")))
    assert data["type"] == "REPLY_END"
    assert data["error"] == "ModelError"


def test_sse_frame_is_json() -> None:
    frame = _sse_frame({"type": "X", "v": 1})
    assert frame.startswith("data: ")
    assert json.loads(frame[6:]) == {"type": "X", "v": 1}


def test_recorder_repeat_start_and_missing_pending(tmp_path: Path) -> None:
    rec = RunRecorder(tmp_path / "t.db")
    first = rec.start("s", "a")
    assert rec.start("s", "a") == first  # 幂等：重复 start 返回同一 id
    rec.consume({"type": "TOOL_CALL_END", "tool_call_id": "no-start"})
    rec.consume({"type": "MODEL_CALL_END", "reply_id": "no-start", "input_tokens": 1})
    assert rec.finish(status="failed") == first


def test_recorder_explicit_failed_and_ok_state(tmp_path: Path) -> None:
    rec = RunRecorder(tmp_path / "t.db")
    rec.start("s", "a")
    rec.consume({"type": "TOOL_CALL_START", "tool_call_id": "t"})
    rec.consume({"type": "TOOL_RESULT_END", "tool_call_id": "t", "state": "failed"})
    rec.consume({"type": "REPLY_END"})  # finished_reason 缺失 → 回退 completed
    run_id = rec.finish(status="failed")
    with repo.connect(tmp_path / "t.db") as conn:
        spans = repo.list_spans(conn, run_id)
        assert len(spans) == 1
        assert spans[0].status == "error"


def test_close_span_without_connection_is_noop(tmp_path: Path) -> None:
    rec = RunRecorder(tmp_path / "t.db")
    # 未 start 就触发需要配对 span 的事件：_close_span 在无 conn 时直接返回
    rec.consume({"type": "MODEL_CALL_END", "reply_id": "orphan", "input_tokens": 5})
    assert rec.finish() == -1


def test_agent_scope_runtime_stream_converts_events() -> None:
    class FakeAgent:
        async def reply_stream(self, msg: object) -> AsyncIterator[object]:
            @dataclass
            class E:
                type: object
                delta: str

            yield E(type=FakeValue("TEXT_BLOCK_DELTA"), delta="hi")

    rt = AgentScopeRuntime(FakeAgent())  # type: ignore[arg-type]
    events = asyncio.run(_collect(rt.stream("s", "q")))
    assert events == [
        {
            "type": "TEXT_BLOCK_DELTA",
            "delta": "hi",
            "created_at": "",
            "session_id": "",
            "reply_id": "",
        }
    ]


async def _collect(it: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item async for item in it]


def test_sse_error_path(monkeypatch: object, tmp_path: Path) -> None:
    class BoomRuntime:
        async def run(self, session_id: str, user_input: str) -> str:
            return ""

        async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
            raise RuntimeError("simulated")
            yield {}  # pragma: no cover

    original = app.state.runtime
    app.state.runtime = BoomRuntime()
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "err.db"))  # type: ignore[attr-defined]
    try:
        with client.stream(
            "POST", "/run/stream", json={"session_id": "s", "user_input": "q"}
        ) as resp:
            body = "".join(resp.iter_text())
        assert '"type": "RUN_ERROR"' in body
        with repo.connect(tmp_path / "err.db") as conn:
            runs = repo.list_runs(conn)
            assert len(runs) == 1
            assert runs[0].status == "failed"
    finally:
        app.state.runtime = original


def test_repeat_start_same_id_with_db(tmp_path: Path) -> None:
    rec = RunRecorder(tmp_path / "r.db")
    rid = rec.start("s", "in")
    assert isinstance(rid, int)
    assert rec.run_id == rid
    rec.finish()  # 关闭连接，避免 ResourceWarning
