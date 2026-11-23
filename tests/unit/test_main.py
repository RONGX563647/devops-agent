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
    assert resp.json() == {"status": "ok"}


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
