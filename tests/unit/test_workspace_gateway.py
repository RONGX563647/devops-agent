"""工作区 HTTP 契约（SPEC-0021 A2/A4/A5/A13/A14 的网关侧）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.gateway.routes.workspaces import router
from devops_agent.workspace.service import WorkspaceService


def _client(tmp_path: Path, **policy: object) -> tuple[TestClient, WorkspaceService]:
    defaults: dict[str, object] = {
        "enabled": True,
        "allow_local_import": True,
        "allowed_local_roots": [str(tmp_path)],
    }
    defaults.update(policy)
    service = WorkspaceService(WorkspacePolicy(**defaults), db_path=tmp_path / "obs.db")
    app = FastAPI()
    app.state.workspace_service = service
    app.include_router(router)
    return TestClient(app), service


def test_disabled_returns_404(tmp_path: Path) -> None:
    """A13：enabled=false 时新路由 404。"""
    client, _ = _client(tmp_path, enabled=False)
    assert client.get("/api/workspaces").status_code == 404


def test_create_local_and_detail(tmp_path: Path) -> None:
    """A1：本地创建返回 realpath 与 managed=false。"""
    repo = tmp_path / "code"
    repo.mkdir()
    client, _ = _client(tmp_path)
    created = client.post(
        "/api/workspaces",
        json={"name": "demo", "source": {"kind": "local", "path": str(repo)}},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["managed"] is False
    assert body["root_path"] == str(repo.resolve())
    detail = client.get(f"/api/workspaces/{body['id']}")
    assert detail.status_code == 200


def test_invalid_path_is_400(tmp_path: Path) -> None:
    """A2：白名单外返回 400。"""
    outside = tmp_path.parent / "outside-http"
    outside.mkdir(exist_ok=True)
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/workspaces",
        json={"name": "x", "source": {"kind": "local", "path": str(outside)}},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "workspace_path_invalid"


def test_bind_conflict_409(tmp_path: Path) -> None:
    """A5：已绑定到 A 的会话再绑 B → 409。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    client, service = _client(tmp_path)
    ws_a = service.create_local("a", str(a))
    ws_b = service.create_local("b", str(b))
    ok = client.post("/api/session/s1/workspace", json={"workspace_id": ws_a.id})
    assert ok.status_code == 200
    conflict = client.post("/api/session/s1/workspace", json={"workspace_id": ws_b.id})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "workspace_already_bound"


def test_browse_and_memory_endpoints(tmp_path: Path) -> None:
    """A14：browse 只给目录；长期记忆端点固定 workspace scope。"""
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True)
    client, service = _client(tmp_path)
    listed = client.get("/api/workspaces/browse", params={"path": str(root)})
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["items"]] == ["child"]
    workspace = service.create_local("demo", str(root))
    memory = client.get(f"/api/workspaces/{workspace.id}/memory/long-term")
    assert memory.status_code == 200
    assert memory.json()["scope"] == "workspace"


def test_control_and_browse_navigation(tmp_path: Path) -> None:
    """能力自述与目录导航：白名单根、上级目录、根处不再上溯。

    `control` 必须走独立路由而不是落到 `/{workspace_id}`，
    `parent` 由后端判定，到白名单根就返回 null —— 否则界面会给出必然 400 的「上一级」。
    """
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    client, _ = _client(tmp_path)

    control = client.get("/api/workspaces/control")
    assert control.status_code == 200
    body = control.json()
    assert body["enabled"] is True
    assert body["allowed_local_roots"] == [str(tmp_path.resolve())]
    assert body["allow_git_import"] is True
    assert body["max_workspaces"] >= 1

    inside = client.get("/api/workspaces/browse", params={"path": str(child)}).json()
    assert inside["parent"] == str(root.resolve())
    assert inside["roots"] == [str(tmp_path.resolve())]

    at_root = client.get("/api/workspaces/browse", params={"path": str(tmp_path)}).json()
    assert at_root["parent"] is None


def test_git_source_requires_kind(tmp_path: Path) -> None:
    """判别联合：缺 kind 直接 422，不猜来源。"""
    client, _ = _client(tmp_path)
    resp = client.post("/api/workspaces", json={"name": "x", "source": {"url": "https://a"}})
    assert resp.status_code == 422
