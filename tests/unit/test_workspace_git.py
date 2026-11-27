"""Git 导入与删除语义（SPEC-0021 A3/A11）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.workspace import errors, git
from devops_agent.workspace.service import WorkspaceService


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", cwd=source)
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
        cwd=source,
    )
    bare = tmp_path / "remote.git"
    _git("clone", "-q", "--bare", str(source), str(bare))
    return bare


def _service(tmp_path: Path, **policy: object) -> WorkspaceService:
    defaults: dict[str, object] = {
        "enabled": True,
        "allow_git_import": True,
        "root": str(tmp_path / "managed"),
    }
    defaults.update(policy)
    return WorkspaceService(WorkspacePolicy(**defaults), db_path=tmp_path / "obs.db")


def test_git_import_records_commit_and_disables_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A3：Git 导入 managed=true、base_commit 非空、core.hooksPath 被禁用。"""
    bare = _seed_repo(tmp_path)
    # 远端白名单只放行网络形态；测试用本地裸仓，直接替掉校验函数
    monkeypatch.setattr(git, "validate_remote_url", lambda url: url)
    service = _service(tmp_path)
    workspace = service.create_git("repo", str(bare))
    detail = service.detail(workspace.id)
    assert detail["managed"] is True
    assert detail["source"] == "git"
    assert detail["base_commit"]
    hooks = subprocess.run(
        ["git", "-C", workspace.root_path, "config", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert hooks.stdout.strip() == "/dev/null"


def test_dirty_clone_delete_requires_force_and_writes_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A11：脏的克隆工作区先 409；force 删除前落 patch，目录被清理。"""
    bare = _seed_repo(tmp_path)
    monkeypatch.setattr(git, "validate_remote_url", lambda url: url)
    service = _service(tmp_path)
    workspace = service.create_git("repo", str(bare))
    (Path(workspace.root_path) / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(errors.WorkspaceError) as exc:
        service.delete(workspace.id)
    assert exc.value.code == errors.DIRTY
    result = service.delete(workspace.id, force=True)
    assert result["patch"] is not None
    assert Path(result["patch"]).exists()
    assert not Path(workspace.root_path).exists()


def test_remote_url_forms() -> None:
    """远端只接受 https / ssh / git@host:path，拒绝 file:// 与本地路径。"""
    assert git.validate_remote_url("https://example.com/a.git")
    assert git.validate_remote_url("ssh://git@example.com/a.git")
    assert git.validate_remote_url("git@example.com:a.git")
    for bad in ("file:///tmp/a.git", "/tmp/a.git", "http://example.com/a.git"):
        with pytest.raises(errors.WorkspaceError):
            git.validate_remote_url(bad)
