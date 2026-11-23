"""网关层单元测试。"""

from fastapi.testclient import TestClient

from devops_agent import main
from devops_agent.main import app
from devops_agent.runtime.runtime import AgentRuntime

client = TestClient(app)


def test_health() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_run_echo() -> None:
    resp = client.post("/run", json={"session_id": "s1", "user_input": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"result": "[s1] hello"}


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
