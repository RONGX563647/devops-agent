"""本地工作区 = 工作树（`source=worktree`）。

用户要的语义：选一个本地仓库，不要动他的检出 —— 在它上面开一个工作树，Agent 在里面跑。
因此这里钉住三件事：
1. 落盘位置在受管目录，主仓库一个字节没变；
2. 分支是新开的 `agent/*`，不占用用户当前分支（git 也不允许两个工作树同分支）；
3. 删除只注销工作树，主仓库与它的分支都还在。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.workspace import errors
from devops_agent.workspace.service import WorkspaceService


def _git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "user-repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
        cwd=repo,
    )
    return repo


def _service(tmp_path: Path, **policy: object) -> WorkspaceService:
    defaults: dict[str, object] = {
        "enabled": True,
        "allow_local_import": True,
        "allowed_local_roots": [str(tmp_path)],
        "root": str(tmp_path / "managed"),
    }
    defaults.update(policy)
    return WorkspaceService(WorkspacePolicy(**defaults), db_path=tmp_path / "obs.db")


def test_git_repo_gets_a_worktree_instead_of_being_used_in_place(tmp_path: Path) -> None:
    """默认 `auto`：git 仓库开工作树，root_path 指向工作树而不是用户目录。"""
    repo = _seed_repo(tmp_path)
    user_branch = _git("branch", "--show-current", cwd=repo)
    service = _service(tmp_path)

    workspace = service.create_local("demo", str(repo))
    detail = service.detail(workspace.id)

    assert detail["source"] == "worktree"
    assert detail["managed"] is True
    root = Path(detail["root_path"])
    assert root.is_dir()
    assert root != repo.resolve()
    assert root.is_relative_to(tmp_path / "managed")
    assert (root / "README.md").read_text(encoding="utf-8") == "hello\n"
    # 新分支：不抢占用户当前分支（同一分支无法被两个工作树同时检出）
    assert detail["branch"].startswith("agent/demo-")
    assert detail["branch"] != user_branch
    # 主仓库原地未动
    assert _git("branch", "--show-current", cwd=repo) == user_branch
    assert _git("status", "--porcelain", cwd=repo) == ""


def test_two_workspaces_from_same_repo_do_not_collide(tmp_path: Path) -> None:
    """同一个仓库可以开多个工作区：各自独立分支与目录。"""
    repo = _seed_repo(tmp_path)
    service = _service(tmp_path)
    first = service.create_local("one", str(repo))
    second = service.create_local("two", str(repo))
    left, right = service.detail(first.id), service.detail(second.id)
    assert left["root_path"] != right["root_path"]
    assert left["branch"] != right["branch"]


def test_delete_worktree_keeps_the_user_repo(tmp_path: Path) -> None:
    """删除工作区只注销工作树：主仓库、分支与提交都还在。"""
    repo = _seed_repo(tmp_path)
    service = _service(tmp_path)
    workspace = service.create_local("demo", str(repo))
    root = Path(workspace.root_path)

    service.delete(workspace.id, force=True)

    assert not root.exists()
    assert (repo / "README.md").is_file()
    listed = _git("worktree", "list", "--porcelain", cwd=repo)
    assert str(root.resolve()) not in listed
    assert _git("rev-parse", "HEAD", cwd=repo)


def test_non_git_dir_falls_back_to_in_place(tmp_path: Path) -> None:
    """`auto` 对非 git 目录原地引用：没有仓库可开工作树，不该报错。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    service = _service(tmp_path)
    detail = service.detail(service.create_local("plain", str(plain)).id)
    assert detail["source"] == "local"
    assert detail["managed"] is False
    assert detail["root_path"] == str(plain.resolve())


def test_worktree_mode_requires_a_repository(tmp_path: Path) -> None:
    """显式要求工作树但目录不是仓库：400 说清楚，不静默降级。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    service = _service(tmp_path)
    with pytest.raises(errors.WorkspaceError) as exc:
        service.create_local("plain", str(plain), mode="worktree")
    assert exc.value.code == errors.SOURCE_INVALID


def test_in_place_mode_opts_out_of_worktree(tmp_path: Path) -> None:
    """显式 `in_place`：即使用户选的是 git 仓库，也直接在原地跑。"""
    repo = _seed_repo(tmp_path)
    service = _service(tmp_path)
    detail = service.detail(service.create_local("demo", str(repo), mode="in_place").id)
    assert detail["source"] == "local"
    assert detail["root_path"] == str(repo.resolve())


def test_old_database_accepts_worktree_source_after_migration(tmp_path: Path) -> None:
    """老库的 CHECK 只有 local/git：迁移后要能写 worktree，且旧行不丢。

    真实升级路径就是这条 —— 老库不重建的话，`create_local` 会在 INSERT 时撞
    CHECK constraint，表现为「新建工作区失败」，而不是一句能看懂的报错。
    """
    import sqlite3

    from devops_agent.observability.repository import db

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL DEFAULT 'local',
            root_path TEXT NOT NULL, default_profile TEXT NOT NULL DEFAULT 'workspace',
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, archived_at TEXT
        );
        CREATE TABLE workspace_repositories (
            workspace_id   TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
            source         TEXT NOT NULL CHECK (source IN ('local','git')),
            managed        INTEGER NOT NULL DEFAULT 0,
            remote_url     TEXT NOT NULL DEFAULT '',
            branch         TEXT NOT NULL DEFAULT '',
            base_commit    TEXT NOT NULL DEFAULT '',
            credential_ref TEXT NOT NULL DEFAULT '',
            detected_git   INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL
        );
        INSERT INTO workspaces (id, name, root_path, created_at, updated_at)
            VALUES ('ws_old', 'old', '/tmp/old', 'now', 'now');
        INSERT INTO workspace_repositories (workspace_id, source, created_at)
            VALUES ('ws_old', 'local', 'now');
        """
    )
    conn.commit()

    db.init_db(conn)

    kept = conn.execute(
        "SELECT source FROM workspace_repositories WHERE workspace_id='ws_old'"
    ).fetchone()
    assert kept["source"] == "local"
    conn.execute(
        "INSERT INTO workspace_repositories (workspace_id, source, created_at) "
        "VALUES ('ws_new', 'worktree', 'now')"
    )
    conn.commit()
    written = conn.execute(
        "SELECT source FROM workspace_repositories WHERE workspace_id='ws_new'"
    ).fetchone()
    assert written["source"] == "worktree"
    conn.close()
