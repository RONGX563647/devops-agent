"""权限端点测试：档位校验、挂起、确认幂等、pending 归属校验。

用一个「第一次调用就挂起」的假运行时驱动完整链路，不发真实模型请求。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from devops_agent.gateway.app import app
from devops_agent.gateway.routes.confirm import confirm_run
from devops_agent.gateway.routes.schemas import ConfirmRequest
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
    # 测试断言的是「默认白名单」，不能继承本机 .env 里显式开放的档位
    monkeypatch.delenv("DEVOPS_AGENT_PERMISSION_PROFILES", raising=False)
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


def test_permissions_listing_includes_unrestricted(park_runtime: ParkRuntime) -> None:
    resp = client.get("/api/permissions")
    assert resp.status_code == 200
    data = resp.json()
    keys = [item["key"] for item in data["available"]]
    assert data["default"] == "workspace"
    assert "unrestricted" in keys
    assert {"key", "label", "note", "fallback", "write"} <= set(data["available"][0])


def test_requesting_unavailable_profile_is_403(
    park_runtime: ParkRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收窄可用集后，越权档位必须显式拒绝，不能静默降级 —— 否则用户以为自己在放开档。"""
    monkeypatch.setenv("DEVOPS_AGENT_PERMISSION_PROFILES", "readonly,workspace,execute,auto")
    resp = client.post(
        "/run/stream",
        json={"session_id": "s1", "user_input": "hi", "permission": "unrestricted"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "permission_not_available"


def test_switch_permission_applies_to_live_session(
    park_runtime: ParkRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行中切档走热替换：实例不变，且能说明生效范围。"""
    seen: list[str] = []
    monkeypatch.setattr(
        park_runtime,
        "apply_profile",
        lambda profile, *, web_search=None: seen.append(profile.key),
        raising=False,
    )
    _post_stream({"session_id": "s1", "user_input": "hi", "permission": "workspace"})
    resp = client.post(
        "/api/session/s1/permission",
        json={"session_id": "s1", "permission": "readonly"},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    assert resp.json()["effective_from"] == "下一次工具判定与下一次模型调用"
    assert seen == ["readonly"]


def test_switch_permission_validates_whitelist_and_session(
    park_runtime: ParkRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切档不是提权后门：未开放档仍 403，路径与 body 的会话必须一致。"""
    monkeypatch.setenv("DEVOPS_AGENT_PERMISSION_PROFILES", "readonly,workspace,execute,auto")
    forbidden = client.post(
        "/api/session/s1/permission",
        json={"session_id": "s1", "permission": "unrestricted"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "permission_not_available"

    mismatch = client.post(
        "/api/session/s1/permission",
        json={"session_id": "s2", "permission": "readonly"},
    )
    assert mismatch.status_code == 400
    # 400 是「请求自相矛盾」（路径与 body 各说一套），与「不是你的会话」（403）有意区分：
    # 前者是调用方拼错，后者是越权，前端与排障需要的反应不同。
    assert mismatch.json()["detail"] == "request_mismatch"


def test_switch_permission_without_live_session_is_honest(
    park_runtime: ParkRuntime,
) -> None:
    """没有正在跑的实例时不谎称立即生效：下一轮按新档装配。"""
    resp = client.post(
        "/api/session/nobody/permission",
        json={"session_id": "nobody", "permission": "readonly"},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    assert resp.json()["effective_from"] == "下一轮"


def test_confirm_honors_permission_switched_while_parked(
    park_runtime: ParkRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认卡片上切档后点允许：按新档执行，不再锁死发起轮。"""
    seen: list[str] = []
    monkeypatch.setattr(
        park_runtime,
        "apply_profile",
        lambda profile, *, web_search=None: seen.append(profile.key),
        raising=False,
    )
    run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "删掉 dist"}))["run_id"])
    with client.stream(
        "POST",
        f"/run/{run_id}/confirm",
        json={
            "session_id": "s1",
            "results": [{"tool_call_id": "c1", "confirmed": True}],
            "permission": "readonly",
        },
    ) as resp:
        assert resp.status_code == 200, resp.read()
        body = "".join(resp.iter_text())

    assert "event: done" in body
    assert seen == ["readonly"]


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


class StuckRuntime(ParkRuntime):
    """模拟框架的保护逻辑：挂起没结清就再开跑，会抛「received no event」。

    真实报错来自 `Agent._check_incoming_event`：它数着还有 N 个调用在等结果，
    而新事件里没有确认结果，于是直接抛错，整轮变成 RUN_ERROR。
    假运行时必须照着抛，否则「结清挂起」这段代码被删掉测试也发现不了。
    """

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        if self.pending:
            raise RuntimeError(
                f"Agent is waiting for {len(self.pending)} tool calls and external execution "
                "results for 0 tool calls, but received no event."
            )
        async for frame in super().stream(session_id, user_input):
            yield frame


def test_new_message_after_park_settles_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """卡片弹出后没作答就发新消息：先把旧的按拒绝结清，新版照常跑起来。

    这是用户报的「运行失败 / Agent is waiting for 1 tool calls ... but received
    no event」的直接回归测试。
    """
    original = app.state.runtime
    runtime = StuckRuntime()
    app.state.runtime = runtime
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "settle.db"))
    try:
        _park(_post_stream({"session_id": "s1", "user_input": "删掉 dist"}))
        body = _post_stream({"session_id": "s1", "user_input": "算了，换一个"})
    finally:
        app.state.runtime = original
        app.state.sessions = None
        app.state.sessions_base = None

    assert "RUN_ERROR" not in body, body
    assert "received no event" not in body
    # 结清必须是拒绝：没批准的调用不能因为「又发了一条消息」被放行
    assert runtime.resumed and all(item.confirmed is False for item in runtime.resumed[0])


def test_confirm_disconnect_marks_run_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """续跑途中断连：run 必须落终态，不能停在「已解挂、ended_at 为空」。

    这是改造前真实存在的缺口 —— 收尾逻辑有两份，主链路有断连兜底、确认续跑没有。
    现在两者共用 `lease.SessionLease.close()`，本用例钉住这条。
    模拟方式与主链路一致：直接驱动生成器，`aclose()` 触发 GeneratorExit
    （它不是 Exception，只有 finally 能兜住）。
    """
    original = app.state.runtime
    app.state.runtime = ParkRuntime()
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "resume-abort.db"))
    try:
        run_id = int(_park(_post_stream({"session_id": "s1", "user_input": "删掉 dist"}))["run_id"])

        async def drive() -> str:
            request = Request({"type": "http", "headers": [], "app": app})
            response = await confirm_run(
                run_id,
                ConfirmRequest(
                    session_id="s1",
                    results=[{"tool_call_id": "c1", "confirmed": True}],
                ),
                request,
            )
            iterator = cast(AsyncIterator[str], response.body_iterator)
            await iterator.__anext__()  # 首帧注释心跳
            frame = await iterator.__anext__()  # resume 的第一条 data 帧
            await iterator.aclose()  # 模拟客户端断开
            return frame

        frame = asyncio.run(drive())
    finally:
        app.state.runtime = original
        app.state.sessions = None
        app.state.sessions_base = None

    assert frame.startswith("data: ")
    with repo.connect() as conn:
        record = repo.get_run(conn, run_id)
    assert record is not None
    assert record.status == "failed", "断连不能留下 running 的悬空 run"
    assert record.ended_at, "终态必须有结束时间，否则 run 列表会把这条算成在跑"
