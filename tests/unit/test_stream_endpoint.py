"""SSE 流式端点测试（用 EchoRuntime + 临时库，端到端验证流式与落库）。"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from devops_agent.gateway.app import app
from devops_agent.gateway.schemas import RunRequest
from devops_agent.gateway.stream import run_stream
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
    # 七个阶段：normalize / detect / expand / attach / redact / budget / compose
    assert parse_types.count("INPUT_PARSE_STAGE") == 7
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


def test_run_stream_uses_attachment_text(
    echo_runtime: None, monkeypatch: object, tmp_path: Path
) -> None:
    """附件正文必须真的进到模型输入里 —— 而不是只出现在轨迹上。"""
    from devops_agent.input import attachments as att

    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "att.db"))  # type: ignore[attr-defined]
    monkeypatch.setenv(att.ATTACHMENT_DIR_ENV, str(tmp_path / "files"))  # type: ignore[attr-defined]
    saved = att.AttachmentStore().save("note.md", "附件里的正文".encode())

    with client.stream(
        "POST",
        "/run/stream",
        json={"session_id": "s1", "user_input": "总结一下", "attachments": [saved.id]},
    ) as resp:
        body = "".join(resp.iter_text())

    frames = _frames(body)
    # Echo 把输入原样回显：回复里带不带附件正文，直接说明它有没有进上下文
    echoed = "".join(str(frame.get("delta", "")) for frame in frames)
    assert "附件里的正文" in echoed
    attach = next(frame for frame in frames if frame.get("stage") == "attach")
    assert attach["items"] == 1
    assert attach["summary"] == "1 份附件已读取"

    # 落库的 run.input 仍是原始输入：加工后的上下文只活在事件与 stage 里
    run_id = int(frames[-1]["run_id"])
    with repo.connect(tmp_path / "att.db") as conn:
        assert repo.get_run(conn, run_id).input == "总结一下"  # type: ignore[union-attr]


def test_run_stream_keeps_secret_out_of_model_input(
    echo_runtime: None, monkeypatch: object, tmp_path: Path
) -> None:
    """端到端：密钥既不能进模型输入，又必须被加密留在一个能取到的地方。"""
    from cryptography.fernet import Fernet

    from devops_agent.secrets import vault as vault_mod

    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(tmp_path / "sec.db"))  # type: ignore[attr-defined]
    monkeypatch.setenv(vault_mod.KEY_ENV, Fernet.generate_key().decode())  # type: ignore[attr-defined]
    vault_mod.reset_vault()
    try:
        with client.stream(
            "POST",
            "/run/stream",
            json={"session_id": "s1", "user_input": f"用 key={secret} 调一下"},
        ) as resp:
            body = "".join(resp.iter_text())

        frames = _frames(body)
        echoed = "".join(str(frame.get("delta", "")) for frame in frames)
        assert secret not in echoed
        assert "{{secret:openai_key}}" in echoed
        # 明文仍在保险箱里，工具边界才解得开
        assert vault_mod.get_vault().get("openai_key") == secret
        # 观测库与对外 API 都不能残留明文：这两个面曾经只挡住模型侧。
        run_id = int(frames[-1]["run_id"])
        with repo.connect(tmp_path / "sec.db") as conn:
            stored = repo.get_run(conn, run_id)
        assert stored is not None
        assert secret not in stored.input
        assert "{{secret:openai_key}}" in stored.input
        history = client.get("/api/runs").json()["items"]
        assert all(secret not in str(item.get("input", "")) for item in history)
    finally:
        vault_mod.reset_vault()


def test_client_disconnect_marks_run_finished(
    echo_runtime: None, monkeypatch: object, tmp_path: Path
) -> None:
    """客户端中途断连：run 必须收尾，不能永远停在 running。

    直接驱动生成器模拟断连：GeneratorExit 不是 Exception，原来的 except 抓不到，
    只会走 finally；这里断言 finally 的兜底 finish 真的把终态写进了库。
    终态用 failed + 明确的 error 文案，而不是新增 status 值：runs.status 的 CHECK
    是建表时定的，老库不会因为改 DDL 放宽，写新值会直接 IntegrityError。
    """
    db = tmp_path / "abort.db"
    monkeypatch.setenv("DEVOPS_AGENT_DB_PATH", str(db))  # type: ignore[attr-defined]

    async def drive() -> str:
        request = Request({"type": "http", "headers": [], "app": app})
        # 直接调路由不会经过 FastAPI 的依赖注入，X-Request-ID 要显式给 None，
        # 否则拿到的是 Header 默认对象，写库时会绑定失败。
        response = await run_stream(RunRequest(session_id="s1", user_input="hi"), request, None)
        iterator = cast(AsyncIterator[str], response.body_iterator)
        await iterator.__anext__()  # 首帧注释心跳
        # 第二帧时 recorder.start 已执行，run 已经落库为 running
        frame = await iterator.__anext__()
        await iterator.aclose()  # 模拟客户端断开
        return frame

    first_data = asyncio.run(drive())
    assert first_data.startswith("data: ")

    with repo.connect(db) as conn:
        runs = repo.list_runs(conn)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "ClientDisconnected" in str(runs[0].error)
