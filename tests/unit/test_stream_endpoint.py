"""SSE 流式端点测试（用 EchoRuntime + 临时库，端到端验证流式与落库）。"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    finally:
        vault_mod.reset_vault()
