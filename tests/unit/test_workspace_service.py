"""工作区服务验收（SPEC-0021 A1/A2/A4/A5/A11/A12/A14）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.workspace import errors
from devops_agent.workspace.service import WorkspaceService


def _service(tmp_path: Path, **policy: object) -> WorkspaceService:
    defaults: dict[str, object] = {
        "enabled": True,
        "allow_local_import": True,
        "allowed_local_roots": [str(tmp_path)],
    }
    defaults.update(policy)
    return WorkspaceService(WorkspacePolicy(**defaults), db_path=tmp_path / "obs.db")


def test_create_local_is_managed_false_and_realpath(tmp_path: Path) -> None:
    """A1：白名单内的本地目录 → managed=false，root_path 是 realpath。"""
    repo = tmp_path / "code"
    repo.mkdir()
    service = _service(tmp_path)
    workspace = service.create_local("demo", str(repo))
    detail = service.detail(workspace.id)
    assert workspace.root_path == str(repo.resolve())
    assert detail["managed"] is False
    assert detail["source"] == "local"
    assert detail["status"] == "active"


def test_local_outside_whitelist_and_symlink_escape_rejected(tmp_path: Path) -> None:
    """A2：白名单外 / 符号链接逃逸 / `..` 全部拒绝。"""
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    service = _service(tmp_path)
    with pytest.raises(errors.WorkspaceError) as exc:
        service.create_local("x", str(outside))
    assert exc.value.code == errors.PATH_INVALID
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(errors.WorkspaceError) as exc2:
        service.create_local("y", str(link))
    assert exc2.value.code == errors.PATH_INVALID
    with pytest.raises(errors.WorkspaceError) as exc3:
        service.create_local("z", str(tmp_path / "code" / ".." / ".."))
    assert exc3.value.code == errors.PATH_INVALID


def test_local_import_requires_flag_and_roots(tmp_path: Path) -> None:
    """白名单未开或 flag 关时本地创建 403。"""
    repo = tmp_path / "code"
    repo.mkdir()
    disabled = WorkspaceService(
        WorkspacePolicy(enabled=True, allow_local_import=False),
        db_path=tmp_path / "a.db",
    )
    with pytest.raises(errors.WorkspaceError) as exc:
        disabled.create_local("x", str(repo))
    assert exc.value.code == errors.LOCAL_DISABLED
    no_roots = WorkspaceService(
        WorkspacePolicy(enabled=True, allow_local_import=True),
        db_path=tmp_path / "b.db",
    )
    with pytest.raises(errors.WorkspaceError):
        no_roots.create_local("x", str(repo))


def test_duplicate_name_and_root_conflict(tmp_path: Path) -> None:
    """A4：同名 active 409；同一 realpath 被第二个工作区引用 409。"""
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    service = _service(tmp_path)
    service.create_local("dup", str(first))
    with pytest.raises(errors.WorkspaceError) as exc:
        service.create_local("dup", str(second))
    assert exc.value.code == errors.NAME_TAKEN
    with pytest.raises(errors.WorkspaceError) as exc2:
        service.create_local("other", str(first))
    assert exc2.value.code == errors.PATH_TAKEN


def test_bind_and_resolve_rejects_rebinding(tmp_path: Path) -> None:
    """A5：会话绑定后请求不同工作区 → 409；未绑定请求返回 None。"""
    repo = tmp_path / "a"
    repo.mkdir()
    other = tmp_path / "b"
    other.mkdir()
    service = _service(tmp_path)
    ws = service.create_local("a", str(repo))
    second = service.create_local("b", str(other))
    assert service.resolve("s1", None) is None
    service.bind_session("s1", ws.id)
    with pytest.raises(errors.WorkspaceError) as exc:
        service.resolve("s1", second.id)
    assert exc.value.code == errors.ALREADY_BOUND
    ref = service.resolve("s1", None)
    assert ref is not None and ref.root_path == ws.root_path


def test_archive_blocks_bind_but_keeps_memory(tmp_path: Path) -> None:
    """A12：归档后新绑定 409；长期记忆仍可读。"""
    from devops_agent.observability import repository as repo

    code = tmp_path / "a"
    code.mkdir()
    service = _service(tmp_path)
    ws = service.create_local("a", str(code))
    with repo.connect(tmp_path / "obs.db") as conn:
        repo.init_db(conn)
        repo.save_long_term(
            conn,
            scope="workspace",
            scope_id=f"workspace:{ws.id}",
            kind="fact",
            text="关键事实",
            ttl_days=30,
            max_entries=100,
        )
    service.archive(ws.id)
    with pytest.raises(errors.WorkspaceError) as exc:
        service.bind_session("s2", ws.id)
    assert exc.value.code == errors.ARCHIVED
    assert service.memory_items(ws.id)["total"] == 1


def test_delete_local_reference_keeps_user_directory(tmp_path: Path) -> None:
    """A11：删除本地引用只注销登记，目录仍在。"""
    repo = tmp_path / "keep"
    repo.mkdir()
    (repo / "file.txt").write_text("x", encoding="utf-8")
    service = _service(tmp_path)
    ws = service.create_local("keep", str(repo))
    result = service.delete(ws.id)
    assert result["deleted"] is True
    assert result["root_removed"] is False
    assert (repo / "file.txt").exists()


def test_delete_with_bound_sessions_needs_force(tmp_path: Path) -> None:
    """有会话持有时删除 409，force 才允许。"""
    repo = tmp_path / "a"
    repo.mkdir()
    service = _service(tmp_path)
    ws = service.create_local("a", str(repo))
    service.bind_session("s1", ws.id)
    with pytest.raises(errors.WorkspaceError) as exc:
        service.delete(ws.id)
    assert exc.value.code == errors.IN_USE
    assert service.delete(ws.id, force=True)["deleted"] is True


def test_browse_only_lists_whitelisted_dirs(tmp_path: Path) -> None:
    """A14：browse 只列白名单内目录，不返回文件内容。"""
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True)
    (root / "file.txt").write_text("secret", encoding="utf-8")
    outside = tmp_path.parent / "outside-browse"
    outside.mkdir(exist_ok=True)
    (root / "escape").symlink_to(outside, target_is_directory=True)
    service = _service(tmp_path)
    result = service.browse(str(root))
    names = [item["name"] for item in result["items"]]
    assert names == ["child"]
    assert all(set(item) == {"name", "path"} for item in result["items"])


def test_disabled_service_refuses_everything(tmp_path: Path) -> None:
    """enabled=false：所有入口都 workspace_disabled（路由层翻成 404）。"""
    service = WorkspaceService(WorkspacePolicy(enabled=False), db_path=tmp_path / "o.db")
    with pytest.raises(errors.WorkspaceError) as exc:
        service.list()
    assert exc.value.code == errors.DISABLED


def test_limit_and_update(tmp_path: Path) -> None:
    """max_workspaces 生效；改名成功、重名 409。"""
    service = _service(tmp_path, max_workspaces=2)
    first = tmp_path / "a"
    first.mkdir()
    ws = service.create_local("a", str(first))
    second = tmp_path / "b"
    second.mkdir()
    service.create_local("b", str(second))
    third = tmp_path / "c"
    third.mkdir()
    with pytest.raises(errors.WorkspaceError) as exc:
        service.create_local("c", str(third))
    assert exc.value.code == errors.LIMIT
    renamed = service.update(ws.id, name="a2")
    assert renamed.name == "a2"
    assert service.update(ws.id, default_profile="readonly").default_profile == "readonly"
    with pytest.raises(errors.WorkspaceError) as exc2:
        service.update(ws.id, name="b")
    assert exc2.value.code == errors.NAME_TAKEN


def test_restore_and_missing_detail(tmp_path: Path) -> None:
    """归档可恢复；不存在返回 404 码。"""
    repo = tmp_path / "a"
    repo.mkdir()
    service = _service(tmp_path)
    ws = service.create_local("a", str(repo))
    service.archive(ws.id)
    assert service.archive(ws.id).status == "archived"  # 幂等
    restored = service.restore(ws.id)
    assert restored.status == "active"
    assert service.restore(ws.id).status == "active"  # 已 active 时也是幂等
    with pytest.raises(errors.WorkspaceError) as exc:
        service.detail("ws_missing")
    assert exc.value.code == errors.NOT_FOUND


def test_resolve_with_requested_auto_binds(tmp_path: Path) -> None:
    """显式 workspace_id 首次出现即绑定；后续未带 id 也沿用该工作区。"""
    repo = tmp_path / "a"
    repo.mkdir()
    service = _service(tmp_path)
    ws = service.create_local("a", str(repo))
    ref = service.resolve("s1", ws.id)
    assert ref is not None and ref.id == ws.id
    again = service.resolve("s1", None)
    assert again is not None and again.id == ws.id


def test_browse_disabled_when_local_import_off(tmp_path: Path) -> None:
    """本地导入关闭时 browse 403。"""
    service = WorkspaceService(
        WorkspacePolicy(
            enabled=True,
            allow_local_import=False,
            allowed_local_roots=[str(tmp_path)],
        ),
        db_path=tmp_path / "o.db",
    )
    with pytest.raises(errors.WorkspaceError) as exc:
        service.browse(str(tmp_path))
    assert exc.value.code == errors.LOCAL_DISABLED


def test_control_reports_capabilities(tmp_path: Path) -> None:
    """control 自述开关与 git 可用性（/health 用）。"""
    control = _service(tmp_path).control()
    assert control["enabled"] is True
    assert control["allow_local_import"] is True
    assert "git_available" in control
