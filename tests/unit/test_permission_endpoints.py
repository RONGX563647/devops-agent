"""权限端点测试：档位校验、挂起、确认幂等、pending 归属校验。

用一个「第一次调用就挂起」的假运行时驱动完整链路，不发真实模型请求。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devops_agent.main import app
from devops_agent.observability import repository as repo

client = TestClient(app)


class ParkRuntime:
    """第一次 stream 结束时留下一条待确认调用；resume 后清空并结束。"""

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.resumed: list[list[Any]] = []

    async def run(self, session_id: str, user_input: str) -> str:
        return user_input

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "先删掉 dist"}
        self.pending = [
            {"tool_call_id": "c1", "name": "Bash", "input": "rm -rf dist", "risk": "danger"}
        ]

    def pending_calls(self) -> list[dict[str, Any]]:
        return self.pending

    def snapshot(self) -> str:
        return json.dumps({"pending": self.pending}, ensure_ascii=False)

    async def resume(self, decisions: list[Any]) -> AsyncIterator[dict[str, Any]]:
        self.resumed.append(list(decisions))
        self.pending = []
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "已经删了"}


@pytest.fixture
def park_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ParkRuntime]:
    original = app.state.runtime
    runtime = ParkRuntime()
    app.state.runtime = runtime
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "perm.db"))
    try:
        yield runtime
    finally:
        app.state.runtime = original
        app.state.sessions = None
        app.state.sessions_base = None


def _frames(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def _park(body: str) -> dict[str, Any]:
    parked = [frame for frame in _frames(body) if frame.get("type") == "RUN_PARKED"]
    assert len(parked) == 1, body
    return parked[0]


def _post_stream(payload: dict[str, Any]) -> str:
    with client.stream("POST", "/run/stream", json=payload) as resp:
        assert resp.status_code == 200, resp.read()
        return "".join(resp.iter_text())


def test_permissions_listing_hides_unrestricted(park_runtime: ParkRuntime) -> None:
    resp = client.get("/api/permissions")
    assert resp.status_code == 200
    data = resp.json()
    keys = [item["key"] for item in data["available"]]
    assert data["default"] == "workspace"
    assert "unrestricted" not in keys
    assert {"key", "label", "note", "fallback", "write"} <= set(data["available"][0])


def test_requesting_unavailable_profile_is_403(park_runtime: ParkRuntime) -> None:
    """越权档位必须显式拒绝，不能静默降级 —— 否则用户以为自己在放开档。"""
    resp = client.post(
        "/run/stream",
        json={"session_id": "s1", "user_input": "hi", "permission": "unrestricted"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "permission_not_available"


def test_stream_parks_instead_of_done(park_runtime: ParkRuntime) -> None:
    body = _post_stream({"session_id": "s1", "user_input": "删掉 dist", "permission": "workspace"})
    parked = _park(body)

    assert parked["run_id"] > 0
    assert parked["tool_calls"][0]["tool_call_id"] == "c1"
    assert parked["tool_calls"][0]["risk"] == "danger"
    # 挂起时必须没有 done：前端据此区分「等确认」和「已完成」
    assert "event: done" not in body

    with repo.connect() as conn:
        row = repo.get_pending(conn, int(parked["run_id"]))
    assert row is not None
    assert row["parked"] is True
    assert row["permission"] == "workspace"
    assert json.loads(str(row["pending_json"]))["tool_calls"][0]["name"] == "Bash"


def test_pending_requires_matching_session(park_runtime: ParkRuntime) -> None:
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "x"}))["run_id"])

    other = client.get(f"/run/{run_id}/pending", params={"session_id": "s2"})
    assert other.status_code == 403

    mine = client.get(f"/run/{run_id}/pending", params={"session_id": "s1"})
    assert mine.status_code == 200
    assert mine.json()["status"] == "parked"
    assert mine.json()["tool_calls"][0]["input"] == "rm -rf dist"


def test_pending_unknown_run_is_404(park_runtime: ParkRuntime) -> None:
    assert client.get("/run/9999/pending", params={"session_id": "s1"}).status_code == 404


def test_confirm_resumes_same_run(park_runtime: ParkRuntime) -> None:
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "删掉 dist"}))["run_id"])

    with client.stream(
        "POST",
        f"/run/{run_id}/confirm",
        json={
            "session_id": "s1",
            "results": [{"tool_call_id": "c1", "confirmed": True, "scope": "prefix"}],
        },
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    done = [frame for frame in _frames(body) if frame.get("type") == "RUN_DONE"]
    assert done and done[0]["run_id"] == run_id
    # 高危命令的服务端降级：prefix 请求被改成 once，不会沉淀成永久放行
    assert park_runtime.resumed[0][0].confirmed is True
    assert park_runtime.resumed[0][0].scope == "prefix"

    with repo.connect() as conn:
        row = repo.get_pending(conn, run_id)
        record = repo.get_run(conn, run_id)
    assert row is not None and row["parked"] is False
    assert record is not None and record.status == "success"
    assert "已经删了" in record.output


def test_confirm_is_idempotent(park_runtime: ParkRuntime) -> None:
    """双击「允许一次」只能执行一次。"""
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "删掉 dist"}))["run_id"])
    payload = {
        "session_id": "s1",
        "results": [{"tool_call_id": "c1", "confirmed": True}],
    }
    with client.stream("POST", f"/run/{run_id}/confirm", json=payload) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    second = client.post(f"/run/{run_id}/confirm", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"] == "already_resolved"
    assert len(park_runtime.resumed) == 1


def test_confirm_rejects_empty_results(park_runtime: ParkRuntime) -> None:
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "x"}))["run_id"])
    resp = client.post(f"/run/{run_id}/confirm", json={"session_id": "s1", "results": []})
    assert resp.status_code == 400


def test_bump_asks_counts_per_run(park_runtime: ParkRuntime) -> None:
    """重复 ASK 只能按 run 计数 —— 换写法重试会产生新的 tool_call_id。"""
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "x"}))["run_id"])
    with repo.connect() as conn:
        # 挂起时已经计过一次
        assert repo.bump_asks(conn, run_id) == 2
        assert repo.bump_asks(conn, run_id) == 3


class ReParkRuntime(ParkRuntime):
    """被批准后又要求确认一次：用来验证超限后的自动拒绝兜底。"""

    async def resume(self, decisions: list[Any]) -> AsyncIterator[dict[str, Any]]:
        self.resumed.append(list(decisions))
        if any(item.confirmed for item in decisions):
            self.pending = [
                {"tool_call_id": "c2", "name": "Bash", "input": "rm -rf build", "risk": "danger"}
            ]
            yield {"type": "TEXT_BLOCK_DELTA", "delta": "再确认一次"}
            return
        self.pending = []
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "算了，换个思路"}


def test_ask_limit_auto_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 run 反复确认超过上限后，服务端自动拒绝，不再弹卡片。"""
    original = app.state.runtime
    runtime = ReParkRuntime()
    app.state.runtime = runtime
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "limit.db"))
    try:
        run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "删掉"}))["run_id"])
        with repo.connect() as conn:
            conn.execute("UPDATE runs SET ask_count=3 WHERE id=?", (run_id,))

        with client.stream(
            "POST",
            f"/run/{run_id}/confirm",
            json={"session_id": "s1", "results": [{"tool_call_id": "c1", "confirmed": True}]},
        ) as resp:
            body = "".join(resp.iter_text())
    finally:
        app.state.runtime = original
        app.state.sessions = None
        app.state.sessions_base = None

    assert "RUN_PARKED" not in body
    assert "event: done" in body
    # 第一次是用户的批准，第二次是服务端自动发出的拒绝
    assert runtime.resumed[0][0].confirmed is True
    assert all(item.confirmed is False for item in runtime.resumed[1])


# --- AskUser 问答链路 -------------------------------------------------


class AskRuntime(ParkRuntime):
    """第一次 stream 挂起一个 AskUser 调用；回答后结束。"""

    def __init__(self) -> None:
        super().__init__()
        self.asking = False
        self.answers: list[list[Any]] = []
        self.interrupted = 0

    @staticmethod
    def _questions() -> list[dict[str, Any]]:
        return [
            {
                "question": "用哪个库？",
                "header": "Library",
                "options": [
                    {"label": "httpx", "description": "async 优先"},
                    {"label": "requests", "description": "同步惯用"},
                ],
                "multi_select": False,
            }
        ]

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "先确认一下选型"}
        self.asking = True

    def pending_kind(self) -> str | None:
        if self.asking:
            return "question"
        return "confirm" if self.pending else None

    def pending_questions(self) -> list[dict[str, Any]]:
        if not self.asking:
            return []
        return [
            {
                "tool_call_id": "q1",
                "name": "AskUser",
                "questions": self._questions(),
            }
        ]

    def external_event(self, answers: list[Any]) -> object:
        from devops_agent.runtime.confirm import validate_answers

        if not self.asking:
            raise ValueError("没有匹配的待回答调用")
        for answer in answers:
            validate_answers(self._questions(), answer.answers)
        return object()

    async def resume_external(self, answers: list[Any]) -> AsyncIterator[dict[str, Any]]:
        self.answers.append(list(answers))
        self.asking = False
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "收到答案"}

    async def interrupt(self) -> AsyncIterator[dict[str, Any]]:
        self.interrupted += 1
        self.asking = False
        yield {"type": "TEXT_BLOCK_DELTA", "delta": "已中断"}


@pytest.fixture
def ask_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AskRuntime]:
    original = app.state.runtime
    runtime = AskRuntime()
    app.state.runtime = runtime
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "ask.db"))
    try:
        yield runtime
    finally:
        app.state.runtime = original
        app.state.sessions = None
        app.state.sessions_base = None


def _answer_payload(selected: list[str]) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "answers": [
            {
                "tool_call_id": "q1",
                "answers": [{"question": "用哪个库？", "selected": selected}],
            }
        ],
    }


def test_stream_parks_question_with_questions(ask_runtime: AskRuntime) -> None:
    body = _post_stream({"session_id": "s1", "user_input": "选个库", "permission": "workspace"})
    parked = _park(body)
    assert parked["kind"] == "question"
    assert parked["questions"][0]["tool_call_id"] == "q1"
    assert parked["questions"][0]["questions"][0]["header"] == "Library"
    assert "event: done" not in body

    run_id = int(parked["run_id"])
    pending = client.get(f"/run/{run_id}/pending", params={"session_id": "s1"})
    assert pending.status_code == 200
    assert pending.json()["kind"] == "question"
    assert pending.json()["questions"][0]["name"] == "AskUser"


def test_answer_resumes_same_run(ask_runtime: AskRuntime) -> None:
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "选个库"}))["run_id"])
    with client.stream("POST", f"/run/{run_id}/answer", json=_answer_payload(["httpx"])) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    done = [frame for frame in _frames(body) if frame.get("type") == "RUN_DONE"]
    assert done and done[0]["run_id"] == run_id
    assert ask_runtime.answers[0][0].tool_call_id == "q1"

    with repo.connect() as conn:
        row = repo.get_pending(conn, run_id)
        record = repo.get_run(conn, run_id)
    assert row is not None and row["parked"] is False
    assert record is not None and record.status == "success"
    assert "收到答案" in record.output


def test_answer_validates_session_kind_and_payload(ask_runtime: AskRuntime) -> None:
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "选个库"}))["run_id"])

    wrong_session = client.post(
        f"/run/{run_id}/answer", json={**_answer_payload(["httpx"]), "session_id": "s2"}
    )
    assert wrong_session.status_code == 403

    not_a_question = client.post(
        f"/run/{run_id}/confirm",
        json={"session_id": "s1", "results": [{"tool_call_id": "q1", "confirmed": True}]},
    )
    assert not_a_question.status_code == 409
    assert not_a_question.json()["detail"] == "not_a_confirm"

    empty = client.post(f"/run/{run_id}/answer", json={"session_id": "s1", "answers": []})
    assert empty.status_code == 400
    assert empty.json()["detail"] == "empty_answers"

    invalid = client.post(f"/run/{run_id}/answer", json=_answer_payload(["urllib"]))
    assert invalid.status_code == 400
    assert "不存在的选项" in invalid.json()["detail"]

    # 校验失败不能把 run 从挂起态摘掉，否则用户再答一次会变成 already_resolved
    with repo.connect() as conn:
        row = repo.get_pending(conn, run_id)
    assert row is not None and row["parked"] is True
    assert not ask_runtime.answers


def test_answer_is_idempotent(ask_runtime: AskRuntime) -> None:
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "选个库"}))["run_id"])
    with client.stream("POST", f"/run/{run_id}/answer", json=_answer_payload(["httpx"])) as resp:
        assert resp.status_code == 200
        "".join(resp.iter_text())

    second = client.post(f"/run/{run_id}/answer", json=_answer_payload(["httpx"]))
    assert second.status_code == 409
    assert second.json()["detail"] == "already_resolved"
    assert len(ask_runtime.answers) == 1


def test_interrupt_finishes_pending_run(ask_runtime: AskRuntime) -> None:
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "选个库"}))["run_id"])
    wrong = client.post(f"/run/{run_id}/interrupt", json={"session_id": "s2"})
    assert wrong.status_code == 403

    with client.stream("POST", f"/run/{run_id}/interrupt", json={"session_id": "s1"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: done" in body
    assert ask_runtime.interrupted == 1

    second = client.post(f"/run/{run_id}/interrupt", json={"session_id": "s1"})
    assert second.status_code == 409
    assert second.json()["detail"] == "already_resolved"


def test_interrupt_requires_capable_runtime(park_runtime: ParkRuntime) -> None:
    """Echo / 假运行时没有 interrupt 能力时返回 400，而不是静默成功。"""
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "删掉"}))["run_id"])
    resp = client.post(f"/run/{run_id}/interrupt", json={"session_id": "s1"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "runtime_not_interruptible"
