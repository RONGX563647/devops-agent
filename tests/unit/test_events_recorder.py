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


def test_event_to_dict_keeps_annotation_fields() -> None:
    """hint/source/block_id 必须透传，否则前端拿到的 HINT_BLOCK 是空壳、无法区分同类块。"""
    from dataclasses import dataclass as dc

    @dc
    class FakeHint:
        type: object
        hint: str
        source: str
        block_id: str

    data = event_to_dict(
        FakeHint(
            type=FakeEnum("HINT_BLOCK"),
            hint="已完成分析",
            source="reasoning",
            block_id="b1",
        )
    )
    assert data["hint"] == "已完成分析"
    assert data["source"] == "reasoning"
    assert data["block_id"] == "b1"


def test_event_to_dict_tolerates_missing_fields() -> None:
    @dataclass
    class Minimal:
        type: object

    data = event_to_dict(Minimal(type=FakeEnum("REPLY_START")))
    assert data["type"] == "REPLY_START"
    assert data["created_at"] == ""
    assert data["session_id"] == ""


def test_event_to_dict_uses_native_pydantic_serialization() -> None:
    """真实 AgentScope 事件走 model_dump(mode=json)，输出仍稳定投影。"""
    from agentscope.event import ModelCallEndEvent

    event = ModelCallEndEvent(
        id="e1",
        created_at="2026-11-23T14:00:00+00:00",
        metadata={"k": "v"},
        type="MODEL_CALL_END",
        reply_id="r1",
        input_tokens=100,
        output_tokens=50,
        cache_input_tokens=0,
        cache_creation_input_tokens=0,
        finished_reason="completed",
    )
    data = event_to_dict(event)
    assert data["type"] == "MODEL_CALL_END"
    assert data["reply_id"] == "r1"
    assert data["input_tokens"] == 100
    assert data["output_tokens"] == 50
    assert data["finished_reason"] == "completed"  # 枚举已被原生转成字符串
    assert "id" not in data  # 内部字段被投影层丢弃
    assert "metadata" not in data
    assert data["session_id"] == ""  # 该事件无此字段，稳定补空串


def test_event_to_dict_native_error_flattens_to_type() -> None:
    """原生序列化里 error 是嵌套 dict，投影后仍取 type 字符串，契约不变。"""
    from agentscope.event import ReplyEndEvent
    from agentscope.types._reply import ErrorInfo

    event = ReplyEndEvent(
        id="e2",
        created_at="2026-11-23T14:00:01+00:00",
        metadata={},
        type="REPLY_END",
        session_id="s1",
        reply_id="r1",
        finished_reason="error",
        error=ErrorInfo(type="internal", message="boom"),
    )
    data = event_to_dict(event)
    assert data["type"] == "REPLY_END"
    assert data["error"] == "internal"


def test_event_to_dict_projects_confirm_tool_names() -> None:
    """确认事件只带 tool_calls 数组，工具名必须投影出来，否则前端只能显示「需要确认」。"""
    from agentscope.event import RequireUserConfirmEvent
    from agentscope.message import ToolCallBlock

    event = RequireUserConfirmEvent(
        id="e3",
        created_at="2026-11-23T14:00:00+00:00",
        metadata={},
        type="REQUIRE_USER_CONFIRM",
        reply_id="r1",
        tool_calls=[
            ToolCallBlock(type="tool_call", id="t1", name="Bash", input='{"command": "rm -rf x"}')
        ],
    )
    data = event_to_dict(event)
    assert data["type"] == "REQUIRE_USER_CONFIRM"
    assert data["tool_call_name"] == "Bash"


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
