"""网关层单元测试。"""

import pytest
from fastapi.testclient import TestClient

from devops_agent import main
from devops_agent.main import app
from devops_agent.runtime.runtime import AgentRuntime, EchoRuntime

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_run_echo() -> None:
    """Echo 回显契约：显式注入 EchoRuntime。

    不依赖"环境里没有 API key"——开发者本地 .env 有凭据时，
    select_runtime 会选真实模型，这条测试就会因环境差异而失败。
    """
    original = app.state.runtime
    app.state.runtime = EchoRuntime()
    try:
        resp = client.post("/run", json={"session_id": "s1", "user_input": "hello"})
        assert resp.status_code == 200
        assert resp.json() == {"result": "[s1] hello"}
    finally:
        app.state.runtime = original


def test_run_requires_fields() -> None:
    resp = client.post("/run", json={"session_id": "s1"})
    assert resp.status_code == 422


def test_run_uses_injected_runtime() -> None:
    """替换 app.state.runtime 即可改变 /run 行为，无需改 env 或重启。"""

    class StubRuntime:
        async def run(self, session_id: str, user_input: str) -> str:
            return f"stub:{session_id}:{user_input}"

    stub: AgentRuntime = StubRuntime()
    original = app.state.runtime
    app.state.runtime = stub
    try:
        resp = client.post("/run", json={"session_id": "s9", "user_input": "hi"})
        assert resp.json() == {"result": "stub:s9:hi"}
    finally:
        app.state.runtime = original


def test_select_runtime_is_exposed() -> None:
    assert callable(main.select_runtime)


def test_select_runtime_falls_back_to_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺凭据时选 Echo 自检运行时（不触网）。"""
    monkeypatch.delenv("DEVOPS_AGENT_API_KEY", raising=False)
    assert isinstance(main.select_runtime(), EchoRuntime)
