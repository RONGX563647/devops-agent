"""输入解析的对外契约：事件形状、落库完整性与 API 读取。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent.input import events as ev
from devops_agent.input.pipeline import STAGE_LABELS, prepare
from devops_agent.main import app
from devops_agent.observability import repository as repo
from devops_agent.observability.models import RunRecord
from devops_agent.observability.recorder import RunRecorder

client = TestClient(app)

_STAMP = "2026-11-23T00:00:00+00:00"


def _prepared() -> object:
    return prepare("看一下 ./README.md", workspace_root=Path.cwd(), remote_fetch=False)


def test_event_sequence_is_start_stages_end() -> None:
    frames = ev.events(_prepared(), _STAMP)  # type: ignore[arg-type]
    types = [frame["type"] for frame in frames]
    assert types == [
        "INPUT_PARSE_START",
        *["INPUT_PARSE_STAGE"] * len(STAGE_LABELS),
        "INPUT_PARSE_END",
    ]


def test_stage_event_keeps_stable_keys() -> None:
    frames = ev.events(_prepared(), _STAMP)  # type: ignore[arg-type]
    stage = frames[1]
    for key in (
        "type",
        "block_id",
        "created_at",
        "stage",
        "stage_label",
        "state",
        "summary",
        "before",
        "after",
        "items",
        "ms",
        "changed",
        "failed",
        "detail",
    ):
        assert key in stage, key
    assert isinstance(stage["changed"], bool)
    assert stage["block_id"] == ev.BLOCK_ID


def test_end_event_summarises_the_whole_run() -> None:
    frames = ev.events(_prepared(), _STAMP)  # type: ignore[arg-type]
    end = frames[-1]
    assert "阶段" in str(end["summary"])
    assert "token" in str(end["summary"])
    assert isinstance(end["degraded"], int)


def test_recorder_persists_every_stage_including_skips(tmp_path: Path) -> None:
    db = tmp_path / "trace.db"
    recorder = RunRecorder(db_path=db, domain="debug", model="m")
    recorder.start("s1", "原文")
    prepared = prepare("纯文本", workspace_root=None, remote_fetch=False)
    for frame in ev.events(prepared, _STAMP):
        recorder.consume(frame)
    run_id = recorder.finish()

    with repo.connect(db) as conn:
        stages = repo.list_input_stages(conn, run_id)
    assert [stage.stage for stage in stages] == [stage.stage for stage in prepared.stages]
    assert [stage.seq for stage in stages] == list(range(len(stages)))
    assert any(stage.status == "skip" for stage in stages)  # 无改动阶段照样落库
    assert all(stage.run_id == run_id for stage in stages)


def test_recorder_ignores_stage_when_run_not_started(tmp_path: Path) -> None:
    recorder = RunRecorder(db_path=tmp_path / "x.db", domain="debug", model="m")
    recorder.consume(ev.stage_event(next(iter(prepare("x", workspace_root=None).stages)), _STAMP))
    assert recorder.finish() == -1  # 未开始即未落库，不抛错


@pytest.fixture
def seeded_db(monkeypatch: object, tmp_path: Path) -> Path:
    path = tmp_path / "api.db"
    monkeypatch.setenv(repo.DB_PATH_ENV, str(path))  # type: ignore[attr-defined]
    with repo.connect(path) as conn:
        repo.init_db(conn)
        run_id = repo.create_run(
            conn,
            RunRecord(
                id=None,
                session_id="s1",
                domain="debug",
                status="success",
                input="原文",
                output="搞定",
                started_at=_STAMP,
                ended_at=_STAMP,
                duration_ms=10,
                model="m",
                total_tokens=10,
                health_score=None,
            ),
        )
    recorder = RunRecorder(db_path=path, domain="debug", model="m")
    recorder.start("s2", "原文")
    prepared = prepare("纯文本", workspace_root=None, remote_fetch=False)
    for frame in ev.events(prepared, _STAMP):
        recorder.consume(frame)
    recorder.finish()
    assert run_id > 0
    return path


def test_api_lists_input_stages(seeded_db: Path) -> None:
    with repo.connect(seeded_db) as conn:
        run_id = repo.list_runs(conn)[0].id
    assert run_id is not None
    resp = client.get(f"/api/runs/{run_id}/input-stages")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == len(STAGE_LABELS)
    assert body[0]["stage"] == "normalize"
    assert set(body[0]) >= {"stage", "label", "status", "changed", "summary", "before", "after"}


def test_api_404_for_unknown_run() -> None:
    resp = client.get("/api/runs/999999/input-stages")
    assert resp.status_code == 404
