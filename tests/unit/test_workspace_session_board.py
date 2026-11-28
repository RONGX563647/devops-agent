"""按工作区列会话（SPEC-0021 左栏分组）的 HTTP 契约与聚合口径。

口径要点（都有断言钉住）：
1. 标题取该会话**最早**一条输入——取最新会让追问把会话主题覆盖成一句短问；
2. 时间取**最近**一次运行；刚绑定没跑过的会话回落到 `bound_at`，不能被隐藏；
3. 未归属会话来自 runs，不因绑定表为空而整批消失（老数据可见性）；
4. 活跃工作区排在归档之前。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.gateway.routes.workspaces import router
from devops_agent.observability.models import RunRecord
from devops_agent.observability.repository import runs as runs_repo
from devops_agent.workspace import store
from devops_agent.workspace.service import WorkspaceService


def _client(tmp_path: Path) -> tuple[TestClient, WorkspaceService]:
    service = WorkspaceService(
        WorkspacePolicy(
            enabled=True,
            allow_local_import=True,
            allowed_local_roots=[str(tmp_path)],
        ),
        db_path=tmp_path / "obs.db",
    )
    app = FastAPI()
    app.state.workspace_service = service
    app.include_router(router)
    return TestClient(app), service


def _seed_run(
    service: WorkspaceService,
    session_id: str,
    *,
    text: str,
    started_at: str,
    status: str = "success",
    workspace_id: str = "",
) -> None:
    with store.connect(service.db_path) as conn:
        runs_repo.create_run(
            conn,
            RunRecord(
                id=None,
                session_id=session_id,
                domain="devops",
                status=status,
                input=text,
                output="",
                started_at=started_at,
                ended_at=started_at,
                duration_ms=1,
                model="stub",
                total_tokens=0,
                health_score=None,
                workspace_id=workspace_id,
            ),
        )


def _workspace(service: WorkspaceService, tmp_path: Path, name: str) -> str:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return service.create_local(name, str(root)).id


def test_workspace_sessions_aggregate_title_and_time(tmp_path: Path) -> None:
    """标题取最早输入、last_at/run_count/failed_count 取运行聚合。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("s1", ws_id)
    _seed_run(service, "s1", text="修复登录接口的 500", started_at="2026-11-28T09:00:00+00:00")
    _seed_run(
        service,
        "s1",
        text="再来一轮",
        started_at="2026-11-28T10:00:00+00:00",
        status="failed",
    )

    body = client.get(f"/api/workspaces/{ws_id}/sessions").json()
    item = body["items"][0]
    assert item["session_id"] == "s1"
    assert item["title"] == "修复登录接口的 500"
    assert item["last_at"] == "2026-11-28T10:00:00+00:00"
    assert item["run_count"] == 2
    assert item["failed_count"] == 1
    assert body["total"] == 1


def test_workspace_sessions_unknown_id_404(tmp_path: Path) -> None:
    """不存在的工作区按既有错误码返回 404。"""
    client, _ = _client(tmp_path)
    resp = client.get("/api/workspaces/ws_missing/sessions")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "workspace_not_found"


def test_board_groups_by_workspace_and_keeps_unbound(tmp_path: Path) -> None:
    """分组视图：工作区挂绑定会话；没有归属行的老会话进 unbound。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("s1", ws_id)
    _seed_run(service, "s1", text="工作区里的会话", started_at="2026-11-28T09:00:00+00:00")
    _seed_run(service, "s9", text="还没归属的会话", started_at="2026-11-28T11:00:00+00:00")

    body = client.get("/api/workspaces/sessions").json()
    assert [group["id"] for group in body["items"]] == [ws_id]
    assert body["items"][0]["session_count"] == 1
    assert [item["session_id"] for item in body["items"][0]["sessions"]] == ["s1"]
    assert [item["session_id"] for item in body["unbound"]] == ["s9"]


def test_board_limits_recent_sessions_per_workspace(tmp_path: Path) -> None:
    """`limit` 控制每个工作区挂几条，按最近运行倒序。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    for index in range(3):
        session = f"s{index}"
        service.bind_session(session, ws_id)
        _seed_run(
            service,
            session,
            text=f"第 {index} 个会话",
            started_at=f"2026-11-28T0{index + 1}:00:00+00:00",
        )

    body = client.get("/api/workspaces/sessions?limit=2").json()
    assert body["items"][0]["session_count"] == 3
    assert [item["session_id"] for item in body["items"][0]["sessions"]] == ["s2", "s1"]


def test_board_puts_archived_workspaces_last(tmp_path: Path) -> None:
    """归档工作区仍可见（会话不能凭空消失），但排在活跃之后。"""
    client, service = _client(tmp_path)
    live = _workspace(service, tmp_path, "live")
    old = _workspace(service, tmp_path, "old")
    service.archive(old)

    body = client.get("/api/workspaces/sessions").json()
    assert [group["status"] for group in body["items"]] == ["active", "archived"]
    assert body["items"][0]["id"] == live
    assert body["items"][1]["id"] == old


def test_bound_session_without_run_sorts_by_binding(tmp_path: Path) -> None:
    """刚绑定还没跑的会话要出现，`last_at` 回落到绑定时间。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("fresh", ws_id)

    body = client.get(f"/api/workspaces/{ws_id}/sessions").json()
    item = body["items"][0]
    assert item["session_id"] == "fresh"
    assert item["run_count"] == 0
    assert item["last_at"] == item["bound_at"] != ""


def test_list_reports_session_count(tmp_path: Path) -> None:
    """列表与详情口径一致：列表页不该显示 0 而详情页显示 3。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("s1", ws_id)

    items = client.get("/api/workspaces").json()["items"]
    assert items[0]["session_count"] == 1


def test_rename_session_overrides_derived_title(tmp_path: Path) -> None:
    """改名只覆盖展示标题：后续新输入不能把用户起的名字顶掉。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("s1", ws_id)
    _seed_run(service, "s1", text="原始输入", started_at="2026-11-28T09:00:00+00:00")

    resp = client.patch("/api/workspaces/sessions/s1", json={"title": "  我起的名字  "})
    assert resp.status_code == 200
    assert resp.json()["title"] == "我起的名字"

    _seed_run(service, "s1", text="后续追问", started_at="2026-11-28T10:00:00+00:00")
    item = client.get(f"/api/workspaces/{ws_id}/sessions").json()["items"][0]
    assert item["title"] == "我起的名字"
    assert item["run_count"] == 2


def test_rename_session_rejects_blank_title(tmp_path: Path) -> None:
    """空白标题会把侧栏渲染成一行空气，直接在契约上拒掉。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("s1", ws_id)

    resp = client.patch("/api/workspaces/sessions/s1", json={"title": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "workspace_source_invalid"


def test_rename_unknown_session_404(tmp_path: Path) -> None:
    """没有绑定行 = 没有地方存标题，按 404 返回而不是静默丢弃。"""
    client, _ = _client(tmp_path)
    resp = client.patch("/api/workspaces/sessions/ghost", json={"title": "x"})
    assert resp.status_code == 404


def test_archive_session_hides_it_until_asked(tmp_path: Path) -> None:
    """归档会话默认不出现；显式要 include_archived 才回来，且排在在用会话之后。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("s1", ws_id)
    service.bind_session("s2", ws_id)
    _seed_run(service, "s1", text="要归档的", started_at="2026-11-28T10:00:00+00:00")
    _seed_run(service, "s2", text="继续用的", started_at="2026-11-28T09:00:00+00:00")

    assert client.patch("/api/workspaces/sessions/s1", json={"archived": True}).status_code == 200

    body = client.get(f"/api/workspaces/{ws_id}/sessions").json()
    assert [item["session_id"] for item in body["items"]] == ["s2"]
    assert body["total"] == 1

    board = client.get("/api/workspaces/sessions").json()
    assert board["items"][0]["session_count"] == 1
    assert [item["session_id"] for item in board["items"][0]["sessions"]] == ["s2"]

    wide = client.get(f"/api/workspaces/{ws_id}/sessions?include_archived=true").json()
    # 归档的 last_at 更新，但排序被压到在用的后面
    assert [item["session_id"] for item in wide["items"]] == ["s2", "s1"]
    assert wide["items"][1]["archived_at"] != ""


def test_restore_archived_session(tmp_path: Path) -> None:
    """恢复是幂等的：重复恢复不报错，会话回到默认视图。"""
    client, service = _client(tmp_path)
    ws_id = _workspace(service, tmp_path, "alpha")
    service.bind_session("s1", ws_id)
    client.patch("/api/workspaces/sessions/s1", json={"archived": True})

    assert client.patch("/api/workspaces/sessions/s1", json={"archived": False}).status_code == 200
    assert client.patch("/api/workspaces/sessions/s1", json={"archived": False}).status_code == 200
    body = client.get(f"/api/workspaces/{ws_id}/sessions").json()
    assert [item["session_id"] for item in body["items"]] == ["s1"]
    assert body["items"][0]["archived_at"] == ""
