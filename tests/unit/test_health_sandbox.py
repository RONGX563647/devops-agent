"""`/health.sandbox` 与 required 失败时的 503。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from devops_agent.executor.manager import SandboxUnavailable
from devops_agent.gateway.app import app
from devops_agent.gateway.bootstrap import describe_sandbox

client = TestClient(app)


class _Manager:
    def status(self) -> dict[str, object]:
        return {
            "mode": "docker",
            "effective": "docker",
            "degraded": False,
            "reason": "",
            "workspace_id": "wid-1",
            "image": "python:3.11-slim",
            "host_workdir": "/host/ws",
            "tool_workdir": "/workspace",
            "containers": 1,
        }


def test_describe_sandbox_without_manager_defaults_local() -> None:
    state = SimpleNamespace(sandbox_manager=None, execution=None, sandbox_error=None)
    payload = describe_sandbox(state)
    assert payload["effective"] == "local"
    assert payload["degraded"] is False


def test_describe_sandbox_reports_manager_status() -> None:
    state = SimpleNamespace(sandbox_manager=_Manager(), execution=None, sandbox_error=None)
    payload = describe_sandbox(state)
    assert payload["mode"] == "docker"
    assert payload["workspace_id"] == "wid-1"
    assert payload["containers"] == 1


def test_describe_sandbox_surfaces_required_failure() -> None:
    error = SandboxUnavailable("sandbox_image_build_failed", "boom")
    state = SimpleNamespace(sandbox_manager=None, execution=None, sandbox_error=error)
    payload = describe_sandbox(state)
    assert payload["degraded"] is True
    assert "sandbox_image_build_failed" in payload["reason"]


def test_health_includes_sandbox_section() -> None:
    body = client.get("/health").json()
    assert "sandbox" in body
    assert body["sandbox"]["effective"] in ("local", "docker")


def test_required_failure_returns_503() -> None:
    original = app.state.sandbox_error
    app.state.sandbox_error = SandboxUnavailable("sandbox_unavailable", "no daemon")
    try:
        response = client.post(
            "/run/stream",
            json={"session_id": "s1", "user_input": "hi"},
        )
    finally:
        app.state.sandbox_error = original
    assert response.status_code == 503
    assert response.json()["detail"] == "sandbox_unavailable"


def test_no_sandbox_error_does_not_block() -> None:
    """没有执行面错误时守卫是空操作，请求继续走到后续校验。"""
    original = app.state.sandbox_error
    app.state.sandbox_error = None
    try:
        response = client.post(
            "/run/stream",
            json={"session_id": "s1", "user_input": "hi", "permission": "nonexistent"},
        )
    finally:
        app.state.sandbox_error = original
    assert response.status_code == 403
