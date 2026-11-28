"""工作区创建 —— 两种来源、三种落盘语义，唯一实现。

来源与落盘不是一回事，这里把两者分开：

- **本地引用**（`source=local`）：直接用用户那个目录，`managed=false`，删除不动它；
- **本地工作树**（`source=worktree`）：在用户那个仓库上 `git worktree add` 一份到受管
  目录，`managed=true`。共用一个对象库，主检出不受影响，Agent 的改动也不会脏了用户
  的 working copy —— 这是「选本地目录」的默认语义；
- **远程克隆**（`source=git`）：`git clone` 到本项目工作区目录
  `<managed_root>/<workspace_id>/repo`，`managed=true`。

`managed` 只决定删除语义（注销 vs 清理产物），三种来源共用同一套登记与校验。

独立成模块而不是继续堆在 `service.py`：那边已到 500 行棘轮上限，且「创建一个工作区」
自成一个职责簇（来源判定 + 落盘 + 回滚），与查询/归档/记忆的耦合只有 policy 与库路径。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devops_agent.workspace import errors, git, paths, store
from devops_agent.workspace.models import RepoInfo, Repository, Workspace, new_id, now_iso

if TYPE_CHECKING:  # 只为类型标注；运行期不 import，避免与 service 循环依赖
    from devops_agent.workspace.service import WorkspaceService

_MAX_NAME = 120
# 落盘模式：auto = git 仓库开工作树、其余原地引用
MODES = ("auto", "worktree", "in_place")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def clean_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, "name 不能为空")
    if len(text) > _MAX_NAME:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, f"name 不能超过 {_MAX_NAME} 字符")
    return text


def _slug(name: str) -> str:
    """分支名用的 ASCII 片段：中文名压不出东西时退回 `ws`，不生成空分支。"""
    text = _SLUG_RE.sub("-", name.lower()).strip("-")
    return text[:24].strip("-") or "ws"


def _branch_for(name: str, workspace_id: str) -> str:
    return f"agent/{_slug(name)}-{workspace_id[-4:]}"


def _check_capacity(conn: Any, policy: Any) -> None:
    if store.count_workspaces(conn) >= policy.max_workspaces:
        raise errors.WorkspaceError(errors.LIMIT, f"工作区数量已达上限 {policy.max_workspaces}")


def _unique(conn: Any, name: str, root_path: str | None) -> None:
    taken = store.find_active_by_name(conn, name)
    if taken is not None:
        raise errors.WorkspaceError(errors.NAME_TAKEN, "同名工作区已存在", workspace_id=taken.id)
    if root_path is not None:
        occupied = store.find_active_by_root(conn, root_path)
        if occupied is not None:
            raise errors.WorkspaceError(
                errors.PATH_TAKEN, "该目录已被其他工作区引用", workspace_id=occupied.id
            )


def build(service: WorkspaceService, name: str, **kwargs: Any) -> tuple[Workspace, Repository]:
    """组装登记对象（不写库）；`repository` 由调用方给出判定结果。"""
    now = now_iso()
    workspace_id = kwargs.pop("workspace_id", None) or new_id()
    repository: Repository = kwargs.pop("repository")
    repository = Repository(
        workspace_id=workspace_id,
        source=repository.source,
        managed=repository.managed,
        remote_url=repository.remote_url,
        branch=repository.branch,
        base_commit=repository.base_commit,
        credential_ref=repository.credential_ref,
        detected_git=repository.detected_git,
        created_at=repository.created_at or now,
    )
    return Workspace(
        id=workspace_id,
        name=name,
        owner="local",
        root_path=kwargs.pop("root_path"),
        default_profile=kwargs.pop("default_profile", None) or service.policy.default_profile,
        status="active",
        created_at=now,
        updated_at=now,
    ), repository


def _insert_or_rollback(
    service: WorkspaceService,
    workspace: Workspace,
    repository: Repository,
    undo: Callable[[], None],
) -> None:
    """写登记；失败时回滚已经落盘的东西，不留「目录在、登记不在」。"""
    try:
        with store.connect(service.db_path) as conn:
            store.insert(conn, workspace, repository)
    except Exception as exc:
        undo()
        if isinstance(exc, sqlite3.IntegrityError):
            raise errors.WorkspaceError(errors.NAME_TAKEN, "名称或路径已被占用") from exc
        raise


def local(
    service: WorkspaceService,
    name: str,
    path: str,
    *,
    mode: str = "auto",
    branch: str = "",
    default_profile: str | None = None,
) -> Workspace:
    """引用本地目录：git 仓库默认开工作树，其余原地引用。"""
    policy = service.policy
    if not policy.enabled:
        raise errors.WorkspaceError(errors.DISABLED, "工作区功能未开启")
    if not policy.allow_local_import:
        raise errors.WorkspaceError(
            errors.LOCAL_DISABLED, "部署未开启本地目录导入（allow_local_import=false）"
        )
    if mode not in MODES:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, f"mode 只能是 {'/'.join(MODES)}")
    clean = clean_name(name)
    source_root = paths.resolve_local_root(path, policy.allowed_local_roots)
    info = git.detect(source_root)

    if mode == "worktree" and not info.detected_git:
        raise errors.WorkspaceError(
            errors.SOURCE_INVALID,
            "该目录不是 git 仓库，无法创建 worktree；改选「原地引用」或先 git init",
            path=str(source_root),
        )
    use_worktree = mode == "worktree" or (mode == "auto" and info.detected_git)
    if not use_worktree:
        return _in_place(service, clean, source_root, info, default_profile)
    return _worktree(
        service, clean, source_root, info, branch=branch, default_profile=default_profile
    )


def _in_place(
    service: WorkspaceService,
    name: str,
    root: Path,
    info: RepoInfo,
    default_profile: str | None,
) -> Workspace:
    """原地引用：`managed=false`，删除只注销登记。"""
    with store.connect(service.db_path) as conn:
        _check_capacity(conn, service.policy)
        _unique(conn, name, str(root))
    workspace, repository = build(
        service,
        name,
        root_path=str(root),
        repository=Repository(
            workspace_id="",
            source="local",
            managed=False,
            branch=info.branch,
            base_commit=info.commit,
            detected_git=info.detected_git,
            created_at=now_iso(),
        ),
        default_profile=default_profile,
    )
    _insert_or_rollback(service, workspace, repository, undo=lambda: None)
    return workspace


def _worktree(
    service: WorkspaceService,
    name: str,
    source_root: Path,
    info: RepoInfo,
    *,
    branch: str,
    default_profile: str | None,
) -> Workspace:
    """在本地仓库上开工作树：主检出不动，Agent 在自己的分支上改。"""
    workspace_id = new_id()
    dest = paths.managed_root(service.policy.root) / workspace_id / "worktree"
    target_branch = git.validate_branch(branch or _branch_for(name, workspace_id))
    with store.connect(service.db_path) as conn:
        _check_capacity(conn, service.policy)
        _unique(conn, name, None)
    created = git.worktree_add(
        source_root,
        dest,
        branch=target_branch,
        base=info.commit or "HEAD",
        timeout=service.policy.git_clone_timeout_sec,
    )
    workspace, repository = build(
        service,
        name,
        workspace_id=workspace_id,
        root_path=str(dest.resolve()),
        repository=Repository(
            workspace_id=workspace_id,
            source="worktree",
            managed=True,
            remote_url="",
            branch=created.branch,
            base_commit=created.commit,
            detected_git=True,
            created_at=now_iso(),
        ),
        default_profile=default_profile,
    )
    _insert_or_rollback(
        service,
        workspace,
        repository,
        undo=lambda: git.worktree_remove(source_root, dest),
    )
    return workspace


def clone(
    service: WorkspaceService,
    name: str,
    url: str,
    *,
    branch: str = "",
    credential_ref: str = "",
    depth: int = 1,
    default_profile: str | None = None,
) -> Workspace:
    """把远端克隆到本项目工作区目录（`managed=true`），失败不留半成品与登记。"""
    policy = service.policy
    if not policy.enabled:
        raise errors.WorkspaceError(errors.DISABLED, "工作区功能未开启")
    if not policy.allow_git_import:
        raise errors.WorkspaceError(
            errors.GIT_DISABLED, "部署未开启 Git 导入（allow_git_import=false）"
        )
    clean = clean_name(name)
    remote = git.validate_remote_url(url)
    workspace_id = new_id()
    dest = paths.managed_root(policy.root) / workspace_id / "repo"
    with store.connect(service.db_path) as conn:
        _check_capacity(conn, policy)
        _unique(conn, clean, None)
    secret = service.credential_resolver(credential_ref) if credential_ref else ""
    info = git.clone(
        remote,
        dest,
        branch=branch,
        depth=depth,
        credential=secret or "",
        timeout=policy.git_clone_timeout_sec,
    )
    workspace, repository = build(
        service,
        clean,
        workspace_id=workspace_id,
        root_path=str(dest.resolve()),
        repository=Repository(
            workspace_id=workspace_id,
            source="git",
            managed=True,
            remote_url=remote,
            branch=info.branch,
            base_commit=info.commit,
            credential_ref=credential_ref,
            detected_git=True,
            created_at=now_iso(),
        ),
        default_profile=default_profile,
    )
    _insert_or_rollback(service, workspace, repository, undo=lambda: git.remove_managed_dir(dest))
    return workspace


__all__ = ["MODES", "build", "clean_name", "clone", "local"]
