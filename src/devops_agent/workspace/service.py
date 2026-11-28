"""工作区服务 —— 创建、归档、删除与记忆查询的唯一业务入口。

所有入口（HTTP / CLI）共用这里，不复制判定。安全相关的三件事只在这里收口：
本地路径白名单（`paths.resolve_local_root`）、远端 URL 形态（`git.validate_remote_url`）、
删除语义（`managed` 区分「注销登记」与「清理克隆产物」）。
会话归属与「按工作区检索」在 `workspace.sessions`，这里只做一层无判定委托。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.workspace import creation, errors, git, paths, queries, sessions, store
from devops_agent.workspace.models import (
    RepoInfo,
    Repository,
    Workspace,
    WorkspaceRef,
    WorkspaceSession,
    now_iso,
)


def _now() -> str:
    return now_iso()


def _default_secret(name: str) -> str | None:
    """从 secrets 保险箱取凭据；未装 cryptography 或不存在时返回 None。"""
    if not name:
        return None
    try:
        from devops_agent.secrets import get_vault  # noqa: PLC0415 - 可选依赖，懒加载
    except Exception:  # noqa: BLE001 - 缺依赖时按「无凭据」处理
        return None
    return get_vault().get(name)


@dataclass
class WorkspaceService:
    """工作区用例集合；`policy` 由调用方注入（默认取配置域）。"""

    policy: WorkspacePolicy
    db_path: Path | None = None
    credential_resolver: Callable[[str], str | None] = _default_secret

    # --- 创建 ---------------------------------------------------------
    def create_local(
        self,
        name: str,
        path: str,
        default_profile: str | None = None,
        *,
        mode: str = "auto",
        branch: str = "",
    ) -> Workspace:
        """引用本地目录：git 仓库默认开工作树（`worktree`），其余原地引用（`local`）。

        工作树的落盘、回滚与登记写入在 `workspace.creation.local`，这里只做委托。
        """
        return creation.local(
            self,
            name,
            path,
            mode=mode,
            branch=branch,
            default_profile=default_profile,
        )

    def create_git(
        self,
        name: str,
        url: str,
        *,
        branch: str = "",
        credential_ref: str = "",
        depth: int = 1,
        default_profile: str | None = None,
    ) -> Workspace:
        """克隆远端到本项目工作区目录（`managed=true`），失败不留下半成品与登记。"""
        return creation.clone(
            self,
            name,
            url,
            branch=branch,
            credential_ref=credential_ref,
            depth=depth,
            default_profile=default_profile,
        )

    # --- 查询 ---------------------------------------------------------
    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """列表：工作区 + 仓库来源（前端要显示 source / managed / branch）。

        一并带上 `session_count`：列表页与详情页说的是同一件事，
        列表显示 0 而详情显示 3 会让人以为绑定没生效。
        """
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            items: list[dict[str, Any]] = []
            for workspace in store.list_workspaces(conn, status=status):
                repository = store.get_repository(conn, workspace.id)
                payload = queries.summary(workspace, repository)
                payload["session_count"] = store.count_sessions(conn, workspace.id)
                items.append(payload)
        return items

    def detail(self, workspace_id: str) -> dict[str, Any]:
        """详情：登记、仓库元信息、绑定会话数与长期记忆条数。"""
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            repository = store.get_repository(conn, workspace_id)
            payload = queries.summary(workspace, repository)
            payload["session_count"] = store.count_sessions(conn, workspace_id)
            payload["long_term_count"] = store.count_long_term(conn, queries.scope_id(workspace_id))
        return payload

    # --- 更新与生命周期 -----------------------------------------------
    def update(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        default_profile: str | None = None,
    ) -> Workspace:
        """改名或改默认档位；改名要重新过 active 内唯一。"""
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            fields: dict[str, Any] = {"updated_at": _now()}
            if name is not None:
                clean = creation.clean_name(name)
                if clean != workspace.name:
                    taken = store.find_active_by_name(conn, clean)
                    if taken is not None and taken.id != workspace_id:
                        raise errors.WorkspaceError(errors.NAME_TAKEN, "名称已被占用")
                    fields["name"] = clean
            if default_profile is not None:
                text = str(default_profile).strip()
                if not text:
                    raise errors.WorkspaceError(errors.SOURCE_INVALID, "default_profile 不能为空")
                fields["default_profile"] = text
            try:
                return store.update(conn, workspace, **fields)
            except sqlite3.IntegrityError as exc:
                raise errors.WorkspaceError(errors.NAME_TAKEN, "名称已被占用") from exc

    def archive(self, workspace_id: str) -> Workspace:
        """归档：幂等；归档后新绑定与新 run 被拒，长期记忆仍可读。"""
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            if workspace.status == "archived":
                return workspace
            now = _now()
            return store.update(conn, workspace, status="archived", archived_at=now, updated_at=now)

    def restore(self, workspace_id: str) -> Workspace:
        """取消归档；若名称/路径已被别的 active 工作区占用则 409。"""
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            if workspace.status == "active":
                return workspace
            taken_name = store.find_active_by_name(conn, workspace.name)
            taken_root = store.find_active_by_root(conn, workspace.root_path)
            if taken_name is not None or taken_root is not None:
                raise errors.WorkspaceError(errors.NAME_TAKEN, "名称或路径已被占用，无法恢复")
            try:
                return store.update(
                    conn, workspace, status="active", archived_at=None, updated_at=_now()
                )
            except sqlite3.IntegrityError as exc:
                raise errors.WorkspaceError(errors.NAME_TAKEN, "名称或路径已被占用") from exc

    def delete(self, workspace_id: str, *, force: bool = False) -> dict[str, Any]:
        """删除：本地引用只注销；克隆按脏状态出 patch 或 409（I5）。"""
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            repository = store.get_repository(conn, workspace_id)
            sessions = store.list_sessions(conn, workspace_id)
            if sessions and not force:
                raise errors.WorkspaceError(
                    errors.IN_USE,
                    "仍有会话绑定该工作区；先解绑或 force=true",
                    sessions=[item.session_id for item in sessions],
                )
            root = Path(workspace.root_path)
            patch, root_removed = self._cleanup_managed(repository, root, workspace_id, force=force)
            memory_deleted = store.delete_long_term(conn, queries.scope_id(workspace_id))
            store.delete(conn, workspace_id)
        return {
            "id": workspace_id,
            "deleted": True,
            "managed": bool(repository.managed) if repository is not None else False,
            "root_removed": root_removed,
            "patch": patch,
            "memory_deleted": memory_deleted,
        }

    def _cleanup_managed(
        self,
        repository: Repository | None,
        root: Path,
        workspace_id: str,
        *,
        force: bool,
    ) -> tuple[str | None, bool]:
        """受管产物的删除前处理：脏目录先 409 或落 patch，再清理目录。

        本地引用（`managed=false`）直接返回，绝不触碰用户目录（I5）。
        工作树不能直接 rmtree：主仓库的 `.git/worktrees/<name>` 会留下悬空登记，
        下次 `worktree add` 到同一路径会失败，所以先反查主仓库并正式注销。
        """
        if repository is None or not repository.managed:
            return None, False
        changed = git.status_porcelain(root)
        if changed and self.policy.require_clean_on_delete and not force:
            raise errors.WorkspaceError(
                errors.DIRTY,
                "受管目录有未提交改动；先提交/清理，或 force=true",
                changed_files=changed,
            )
        patch = str(git.write_delete_patch(root, workspace_id)) if changed else None
        if root.exists():
            origin = git.main_root(root) if repository.source == "worktree" else None
            if origin is not None:
                git.worktree_remove(origin, root)
            else:
                git.remove_managed_dir(root)
        return patch, True

    # --- 会话归属与分组 -----------------------------------------------
    def bind_session(self, session_id: str, workspace_id: str) -> WorkspaceSession:
        """显式绑定会话；判定与写入在 `sessions.bind`（同一职责簇已迁出）。"""
        self._require_enabled()
        return sessions.bind(self, session_id, workspace_id)

    def resolve(self, session_id: str, requested_id: str | None = None) -> WorkspaceRef | None:
        """一轮请求的执行目录解析：显式 > 已绑定 > 旧路径（None）。"""
        return sessions.resolve(self, session_id, requested_id)

    def sessions_for(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """某工作区下的会话（左栏展开与详情页共用）。"""
        self._require_enabled()
        return sessions.for_workspace(
            self,
            workspace_id,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )

    def session_board(
        self, *, limit_per_workspace: int = 5, include_archived: bool = False
    ) -> dict[str, Any]:
        """左栏分组视图：工作区底下挂最近会话 + 未归属会话。"""
        self._require_enabled()
        return sessions.board(
            self,
            limit_per_workspace=limit_per_workspace,
            include_archived=include_archived,
        )

    def session_detail(self, session_id: str) -> dict[str, Any]:
        """单条会话（含改名后的标题），给会话页顶栏用。"""
        self._require_enabled()
        return sessions.detail(self, session_id)

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        """改会话标题：只覆盖展示标题，原始输入不动。"""
        self._require_enabled()
        return sessions.rename(self, session_id, title)

    def archive_session(self, session_id: str, *, archived: bool = True) -> dict[str, Any]:
        """归档 / 恢复会话：runs 与归属都保留，只是从默认视图收起。"""
        self._require_enabled()
        return sessions.archive(self, session_id, archived=archived)

    # --- 目录浏览与记忆 -----------------------------------------------
    def browse(self, path: str = "") -> dict[str, Any]:
        """列出白名单内的子目录，供「选择本地目录」使用。

        一并给出 `parent` 与 `roots`：选择器要能展示「上一级」（到白名单根为止）
        和可一键跳转的白名单根，否则界面只能让用户手敲绝对路径。
        """
        self._require_enabled()
        if not self.policy.allow_local_import:
            raise errors.WorkspaceError(errors.LOCAL_DISABLED, "部署未开启本地目录导入")
        roots = self.policy.allowed_local_roots
        base, items = paths.browse_children(path, roots)
        return {
            "root": str(base),
            "items": items,
            "parent": paths.parent_within(base, roots),
            "roots": paths.real_roots(roots),
        }

    def memory_items(
        self, workspace_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """固定 `scope='workspace'` 的长期记忆列表（跨工作区不可见）。"""
        self._require_enabled()
        scope_id = queries.scope_id(workspace_id)
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            items = store.list_long_term(conn, scope_id, limit=limit, offset=offset)
            total = store.count_long_term(conn, scope_id)
        return {
            "workspace_id": workspace_id,
            "scope": "workspace",
            "scope_id": scope_id,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def control(self) -> dict[str, Any]:
        """能力自述：开关、白名单、git 是否可用（/health 与前端共用）。"""
        return queries.control(self.policy, git_available=git.git_available())

    # --- 内部 ---------------------------------------------------------
    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise errors.WorkspaceError(errors.DISABLED, "工作区功能未开启")


__all__ = ["RepoInfo", "WorkspaceService"]
