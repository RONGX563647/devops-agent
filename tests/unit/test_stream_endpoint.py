"""SSE 流式端点测试（用 EchoRuntime + 临时库，端到端验证流式与落库）。"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent import main
from devops_agent.main import app
from devops_agent.observability import repository as repo
from devops_agent.runtime.runtime import EchoRuntime

client = TestClient(app)


@pytest.fixture
def echo_runtime() -> Iterator[None]:
    """用 EchoRuntime 替换注入的 runtime，结束后恢复。

    不使用 monkeypatch.setattr(app.state, ...)：State 的存储机制会让 monkeypatch
    在 teardown 时误删属性，污染后续测试。
    """
    original = app.state.runtime
    app.state.runtime = EchoRuntime()
    try:
        yield
    finally:
        app.state.runtime = original


def _frames(text: str) -> list[dict[str, object]]:
    """解析 SSE 响应体里的 data 帧。"""
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def test_run_stream_emits_frames_and_persists(
    echo_runtime: None, monkeypatch: object, tmp_path: Path
) -> None:
    db = tmp_path / "stream.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))  # type: ignore[attr-defined]

    with client.stream(
        "POST", "/run/stream", json={"session_id": "s1", "user_input": "hi"}
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = "".join(resp.iter_text())

    frames = _frames(body)
    types = [f["type"] for f in frames]
    # 解析事件必须排在模型事件之前：前端据此把「输入被做了什么」画在轮次开头
    assert types[0] == "INPUT_PARSE_START"
    assert types[-4:] == ["REPLY_START", "TEXT_BLOCK_DELTA", "REPLY_END", "RUN_DONE"]
    parse_types = [t for t in types if t.startswith("INPUT_PARSE_")]
    assert parse_types[0] == "INPUT_PARSE_START"
    assert parse_types[-1] == "INPUT_PARSE_END"
    assert parse_types.count("INPUT_PARSE_STAGE") == 6
    done = frames[-1]
    assert done["text"] == "[s1] hi"
    assert done["status"] == "success"
    assert isinstance(done["run_id"], int)

    with repo.connect(db) as conn:
        run = repo.get_run(conn, int(done["run_id"]))
        assert run is not None
        assert run.output == "[s1] hi"
        assert run.status == "success"


def test_run_stream_honors_request_id(
    echo_runtime: None, monkeypatch: object, tmp_path: Path
) -> None:
    db = tmp_path / "idem.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))  # type: ignore[attr-defined]

    ids = []
    for _ in range(2):
        with client.stream(
            "POST",
            "/run/stream",
            json={"session_id": "s1", "user_input": "hi"},
            headers={"X-Request-ID": "fixed"},
        ) as resp:
            body = "".join(resp.iter_text())
        frames = _frames(body)
        ids.append(frames[-1]["run_id"])

    assert ids[0] == ids[1]
    with repo.connect(db) as conn:
        assert len(repo.list_runs(conn)) == 1


def test_run_stream_sends_anti_buffering_headers(
    echo_runtime: None, monkeypatch: object, tmp_path: Path
) -> None:
    """SSE 必须带禁缓冲头，否则代理会把整段流攒到结束才下发。

    这正是"后端在流、前端一次性渲染"的典型成因之一，用测试锁死。
    """
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "hdr.db"))  # type: ignore[attr-defined]

    with client.stream(
        "POST", "/run/stream", json={"session_id": "s1", "user_input": "hi"}
    ) as resp:
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        body = "".join(resp.iter_text())

    # 首帧注释心跳：让代理与浏览器在模型推理空窗期就确认流已建立
    assert body.startswith(": ping")


def test_legacy_run_still_works(echo_runtime: None) -> None:
    """非流式 /run 契约保持不变。"""
    resp = client.post("/run", json={"session_id": "s1", "user_input": "hello"})
    assert resp.json() == {"result": "[s1] hello"}
    assert callable(main.select_runtime)


def test_run_stream_injects_domain_model_from_env(
    echo_runtime: None, monkeypatch: object, tmp_path: Path
) -> None:
    """domain/model 从环境注入，落库记录反映真实运行上下文。"""
    db = tmp_path / "ctx.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))  # type: ignore[attr-defined]
    monkeypatch.setenv("DEVOPS_AGENT_DOMAIN", "delivery")  # type: ignore[attr-defined]
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen3.8-flash")  # type: ignore[attr-defined]

    with client.stream(
        "POST", "/run/stream", json={"session_id": "s1", "user_input": "hi"}
    ) as resp:
        body = "".join(resp.iter_text())
    run_id = int(_frames(body)[-1]["run_id"])

    with repo.connect(db) as conn:
        run = repo.get_run(conn, run_id)
        assert run is not None
        assert run.domain == "delivery"
        assert run.model == "qwen3.8-flash"
