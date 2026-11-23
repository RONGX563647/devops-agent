"""网关层单元测试。"""

from fastapi.testclient import TestClient

from devops_agent.main import app

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
