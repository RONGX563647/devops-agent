"""网关层单元测试。"""

import pytest
from fastapi.testclient import TestClient

from devops_agent import main
from devops_agent.main import app
from devops_agent.runtime.runtime import EchoRuntime

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # 工具依赖就绪状态一并返回：缺 rg 这类问题不该等到模型调用失败才暴露。
    assert "grep" in body["tools"]
    # 运行时模式也要能看出来：Echo 兜底时前端据此挂横幅
    assert body["runtime"]["kind"] in ("echo", "agentscope")


def test_health_reports_echo_degradation() -> None:
    """没配凭据时必须是 echo + 一句可读的说明，而不是默默回显。"""
    original = app.state.runtime
    app.state.runtime = EchoRuntime()
    try:
        body = client.get("/health").json()
    finally:
        app.state.runtime = original
    assert body["runtime"]["kind"] == "echo"
    assert "回显" in body["runtime"]["notice"]


def test_runtime_status_reports_model_for_real_runtime() -> None:
    """真实运行时给模型名、且没有告警文案（免得横幅一直挂着）。"""
    status = main.runtime_status(object())  # type: ignore[arg-type]
    assert status["kind"] == "agentscope"
    assert status["notice"] == ""


def test_run_endpoint_is_removed() -> None:
    """非流式 /run 已下线：输入必须经流式端点走解析与事件落库。"""
    resp = client.post("/run", json={"session_id": "s1", "user_input": "hello"})
    assert resp.status_code == 404


def test_select_runtime_is_exposed() -> None:
    assert callable(main.select_runtime)


def test_select_runtime_falls_back_to_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺凭据时选 Echo 自检运行时（不触网）。"""
    monkeypatch.delenv("DEVOPS_AGENT_API_KEY", raising=False)
    assert isinstance(main.select_runtime(), EchoRuntime)
