"""事件转译与记录器单测（假事件对象，不发真实模型请求）。"""

from dataclasses import dataclass
from pathlib import Path

from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import RunRecorder, _elapsed_ms
from devops_agent.runtime.events import event_to_dict


class FakeEnum:
    def __init__(self, value: str) -> None:
        self.value = value


@dataclass
class FakeModelEnd:
    type: object
    created_at: str
    session_id: str
    reply_id: str
    model_name: str
    input_tokens: int
    output_tokens: int
    finished_reason: object


def test_event_to_dict_converts_enum_and_scalars() -> None:
    event = FakeModelEnd(
        type=FakeEnum("MODEL_CALL_END"),
        created_at="2026-11-23T14:00:00+00:00",
        session_id="s1",
        reply_id="r1",
        model_name="qwen3.8-flash",
        input_tokens=100,
        output_tokens=50,
        finished_reason=FakeEnum("stop"),
    )
    data = event_to_dict(event)
    assert data["type"] == "MODEL_CALL_END"
    assert data["model_name"] == "qwen3.8-flash"
    assert data["input_tokens"] == 100
    assert data["output_tokens"] == 50
    assert data["finished_reason"] == "stop"
    assert "error" not in data


def test_event_to_dict_tolerates_missing_fields() -> None:
    @dataclass
    class Minimal:
        type: object

    data = event_to_dict(Minimal(type=FakeEnum("REPLY_START")))
    assert data["type"] == "REPLY_START"
    assert data["created_at"] == ""
    assert data["session_id"] == ""


def test_elapsed_ms_handles_bad_dates() -> None:
    assert _elapsed_ms("bad", "also-bad") == 0
    assert _elapsed_ms("2026-11-23T14:00:00+00:00", "2026-11-23T14:00:01+00:00") == 1000
    # 混合带时区与不带时区：TypeError 也要吞掉（真实事件流踩过的坑）
    assert _elapsed_ms("2026-11-23T14:00:00", "2026-11-23T14:00:01+00:00") == 0


def _evt(etype: str, **kwargs: object) -> dict[str, object]:
    created = kwargs.pop("created_at", "2026-11-23T14:00:00+00:00")
    return {"type": etype, "created_at": created, **kwargs}


def test_recorder_full_lifecycle(tmp_path: Path) -> None:
    rec = RunRecorder(tmp_path / "t.db", domain="debug", model="qwen", request_id="req-1")
    rec.start("s1", "定位 bug")

    rec.consume(_evt("REPLY_START"))
    rec.consume(
        _evt(
            "MODEL_CALL_START",
            reply_id="r1",
            created_at="2026-11-23T14:00:00+00:00",
            model_name="qwen",
        )
    )
    rec.consume(
        _evt(
            "MODEL_CALL_END",
            reply_id="r1",
            created_at="2026-11-23T14:00:01+00:00",
            input_tokens=100,
            output_tokens=50,
        )
    )
    rec.consume(_evt("TOOL_CALL_START", tool_call_id="t1", created_at="2026-11-23T14:00:01+00:00"))
    rec.consume(
        _evt(
            "TOOL_CALL_END",
            tool_call_id="t1",
            created_at="2026-11-23T14:00:02+00:00",
        )
    )
    rec.consume(_evt("TEXT_BLOCK_DELTA", delta="已定位"))
    rec.consume(_evt("REPLY_END", finished_reason="completed"))

    run_id = rec.finish()
    assert run_id == 1
    assert rec.final_text == "已定位"
    assert rec.token_usage == (100, 50)

    with repo.connect(tmp_path / "t.db") as conn:
        run = repo.get_run(conn, run_id)
        assert run is not None
        assert run.status == "success"
        assert run.output == "已定位"
        assert run.input_tokens == 100
        assert run.output_tokens == 50
        assert run.total_tokens == 150
        assert run.duration_ms is not None and run.duration_ms >= 0

        spans = repo.list_spans(conn, run_id)
        assert [s.type for s in spans] == ["model", "tool"]
        assert spans[0].duration_ms == 1000
        assert spans[0].input_tokens == 100
        assert spans[0].output_tokens == 50
        assert spans[1].status == "ok"


def test_recorder_exceed_max_iters_marks_failed(tmp_path: Path) -> None:
    rec = RunRecorder(tmp_path / "t.db")
    rec.start("s2", "跑不完")
    rec.consume(_evt("EXCEED_MAX_ITERS", reply_id="r1"))
    rec.finish()
    with repo.connect(tmp_path / "t.db") as conn:
        run = repo.get_run(conn, 1)
        assert run is not None
        assert run.status == "failed"


def test_recorder_idempotent_request(tmp_path: Path) -> None:
    rec1 = RunRecorder(tmp_path / "t.db", request_id="same")
    id1 = rec1.start("s", "a")
    rec1.finish()
    rec2 = RunRecorder(tmp_path / "t.db", request_id="same")
    id2 = rec2.start("s", "a")
    assert id1 == id2
    rec2.finish()
    with repo.connect(tmp_path / "t.db") as conn:
        assert len(repo.list_runs(conn)) == 1


def test_recorder_consume_before_start_is_noop(tmp_path: Path) -> None:
    rec = RunRecorder(tmp_path / "t.db")
    rec.consume(_evt("TEXT_BLOCK_DELTA", delta="ignored"))
    assert rec.final_text == ""
    assert rec.finish() == -1
