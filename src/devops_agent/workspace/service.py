"""工作区服务 —— 创建、绑定、解析、归档与删除的唯一业务入口。

所有入口（HTTP / CLI）共用这里，不复制判定。安全相关的三件事只在这里收口：
本地路径白名单（`paths.resolve_local_root`）、远端 URL 形态（`git.validate_remote_url`）、
删除语义（`managed` 区分「注销登记」与「清理克隆产物」）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.workspace import errors, git, paths, store
from devops_agent.workspace.models import (
    RepoInfo,
    Repository,
    Workspace,
    WorkspaceRef,
    WorkspaceSession,
    new_id,
)

_MAX_NAME = 120


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _default_secret(name: str) -> str | None:
    """从 secrets 保险箱取凭据；未装 cryptography 或不存在时返回 None。"""
    if not name:
        return None
    try:
        from devops_agent.secrets import get_vault  # noqa: PLC0415 - 可选依赖，懒加载
    except Exception:  # noqa: BLE001 - 缺依赖时按「无凭据」处理
        return None
    return get_vault().get(name)


def _clean_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, "name 不能为空")
    if len(text) > _MAX_NAME:
        raise errors.WorkspaceError(errors.SOURCE_INVALID, f"name 不能超过 {_MAX_NAME} 字符")
    return text


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
    ) -> Workspace:
        """引用一个已有本地目录（原地引用，`managed=false`，删除不动目录）。"""
        self._require_enabled()
        if not self.policy.allow_local_import:
            raise errors.WorkspaceError(
                errors.LOCAL_DISABLED, "部署未开启本地目录导入（allow_local_import=false）"
            )
        clean = _clean_name(name)
        root = paths.resolve_local_root(path, self.policy.allowed_local_roots)
        info = git.detect(root)
        with store.connect(self.db_path) as conn:
            self._check_capacity(conn)
            self._unique(conn, clean, str(root))
            workspace, repository = self._build_workspace(
                clean,
                root_path=str(root),
                repository=Repository(
                    workspace_id="",
                    source="local",
                    managed=False,
                    branch=info.branch,
                    base_commit=info.commit,
                    detected_git=info.detected_git,
                    created_at=_now(),
                ),
                default_profile=default_profile,
            )
            try:
                store.insert(conn, workspace, repository)
            except sqlite3.IntegrityError as exc:  # 并发下的唯一索引兜底
                raise errors.WorkspaceError(errors.NAME_TAKEN, "名称或路径已被占用") from exc
        return workspace

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
        """克隆远端到受管目录（`managed=true`），失败不留下半成品与登记。"""
        self._require_enabled()
        if not self.policy.allow_git_import:
            raise errors.WorkspaceError(
                errors.GIT_DISABLED, "部署未开启 Git 导入（allow_git_import=false）"
            )
        clean = _clean_name(name)
        remote = git.validate_remote_url(url)
        workspace_id = new_id()
        dest = paths.managed_root(self.policy.root) / workspace_id / "repo"
        with store.connect(self.db_path) as conn:
            self._check_capacity(conn)
            self._unique(conn, clean, None)
        secret = self.credential_resolver(credential_ref) if credential_ref else ""
        info = git.clone(
            remote,
            dest,
            branch=branch,
            depth=depth,
            credential=secret or "",
            timeout=self.policy.git_clone_timeout_sec,
        )
        workspace, repository = self._build_workspace(
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
                created_at=_now(),
            ),
            default_profile=default_profile,
        )
        try:
            with store.connect(self.db_path) as conn:
                store.insert(conn, workspace, repository)
        except Exception as exc:
            git.remove_managed_dir(dest)
            if isinstance(exc, sqlite3.IntegrityError):
                raise errors.WorkspaceError(errors.NAME_TAKEN, "名称已被占用") from exc
            raise
        return workspace

    # --- 查询 ---------------------------------------------------------
    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """列表：工作区 + 仓库来源（前端要显示 source / managed / branch）。"""
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            items: list[dict[str, Any]] = []
            for workspace in store.list_workspaces(conn, status=status):
                repository = store.get_repository(conn, workspace.id)
                items.append(self._summary(workspace, repository))
        return items

    def detail(self, workspace_id: str) -> dict[str, Any]:
        """详情：登记、仓库元信息、绑定会话数与长期记忆条数。"""
        self._require_enabled()
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            repository = store.get_repository(conn, workspace_id)
            payload = self._summary(workspace, repository)
            payload["session_count"] = store.count_sessions(conn, workspace_id)
            payload["long_term_count"] = store.count_long_term(conn, self._scope_id(workspace_id))
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
                clean = _clean_name(name)
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
            memory_deleted = store.delete_long_term(conn, self._scope_id(workspace_id))
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
        """受管克隆的删除前处理：脏目录先 409 或落 patch，再清理目录。

        本地引用（`managed=false`）直接返回，绝不触碰用户目录（I5）。
        """
        if repository is None or not repository.managed:
            return None, False
        changed = git.status_porcelain(root)
        if changed and self.policy.require_clean_on_delete and not force:
            raise errors.WorkspaceError(
                errors.DIRTY,
                "受管克隆目录有未提交改动；先提交/清理，或 force=true",
                changed_files=changed,
            )
        patch = str(git.write_delete_patch(root, workspace_id)) if changed else None
        if root.exists():
            git.remove_managed_dir(root)
        return patch, True

    # --- 会话绑定 -----------------------------------------------------
    def bind_session(self, session_id: str, workspace_id: str) -> WorkspaceSession:
        """显式绑定；已绑到别处一律 409，不做「以最后一次为准」。"""
        self._require_enabled()
        session = str(session_id or "").strip()
        if not session:
            raise errors.WorkspaceError(errors.SOURCE_INVALID, "session_id 不能为空")
        with store.connect(self.db_path) as conn:
            workspace = store.get_workspace(conn, workspace_id)
            if workspace is None:
                raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
            if workspace.status != "active":
                raise errors.WorkspaceError(errors.ARCHIVED, "工作区已归档，不能绑定")
            existing = store.get_session_binding(conn, session)
            if existing is not None:
                if existing.workspace_id != workspace_id:
                    raise errors.WorkspaceError(
                        errors.ALREADY_BOUND,
                        "会话已绑定其他工作区",
                        workspace_id=existing.workspace_id,
                    )
                return existing
            binding = WorkspaceSession(
                session_id=session,
                workspace_id=workspace_id,
                bound_at=_now(),
                bound_by="local",
            )
            store.bind_session(conn, binding)
            store.set_session_workspace(conn, session, workspace_id)
        return binding

    def resolve(self, session_id: str, requested_id: str | None = None) -> WorkspaceRef | None:
        """一轮请求的执行目录解析：显式 > 已绑定 > 旧路径（None）。"""
        if not self.policy.enabled:
            return None
        requested = str(requested_id or "").strip() or None
        with store.connect(self.db_path) as conn:
            binding = store.get_session_binding(conn, session_id)
            if requested is not None:
                return self._bind_and_ref(conn, session_id, requested, binding)
            return self._bound_ref(conn, binding)

    @staticmethod
    def _bind_and_ref(
        conn: Any,
        session_id: str,
        requested: str,
        binding: WorkspaceSession | None,
    ) -> WorkspaceRef:
        """显式 id：校验 active 与归属一致；首次出现即落绑定。"""
        workspace = store.get_workspace(conn, requested)
        if workspace is None:
            raise errors.WorkspaceError(errors.NOT_FOUND, "工作区不存在")
        if workspace.status != "active":
            raise errors.WorkspaceError(errors.ARCHIVED, "工作区已归档")
        if binding is not None and binding.workspace_id != requested:
            raise errors.WorkspaceError(
                errors.ALREADY_BOUND,
                "会话已绑定其他工作区，需新开会话",
                workspace_id=binding.workspace_id,
            )
        if binding is None:
            store.bind_session(
                conn,
                WorkspaceSession(
                    session_id=session_id,
                    workspace_id=requested,
                    bound_at=_now(),
                    bound_by="local",
                ),
            )
            store.set_session_workspace(conn, session_id, requested)
        return WorkspaceRef(id=workspace.id, root_path=workspace.root_path)

    @staticmethod
    def _bound_ref(conn: Any, binding: WorkspaceSession | None) -> WorkspaceRef | None:
        """已绑定：工作区被删则回落旧路径；归档则拒绝。"""
        if binding is None:
            return None
        workspace = store.get_workspace(conn, binding.workspace_id)
        if workspace is None:
            return None  # 工作区已删除：回到旧路径，由调用方提示
        if workspace.status != "active":
            raise errors.WorkspaceError(errors.ARCHIVED, "工作区已归档")
        return WorkspaceRef(id=workspace.id, root_path=workspace.root_path)

    # --- 目录浏览与记忆 -----------------------------------------------
    def browse(self, path: str = "") -> dict[str, Any]:
        """列出白名单内的子目录，供「选择本地目录」使用。"""
        self._require_enabled()
        if not self.policy.allow_local_import:
            raise errors.WorkspaceError(errors.LOCAL_DISABLED, "部署未开启本地目录导入")
        base, items = paths.browse_children(path, self.policy.allowed_local_roots)
        return {"root": str(base), "items": items}

    def memory_items(
        self, workspace_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """固定 `scope='workspace'` 的长期记忆列表（跨工作区不可见）。"""
        self._require_enabled()
        scope_id = self._scope_id(workspace_id)
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
        return {
            "enabled": self.policy.enabled,
            "allow_local_import": self.policy.allow_local_import,
            "allow_git_import": self.policy.allow_git_import,
            "allowed_local_roots": list(self.policy.allowed_local_roots),
            "git_available": git.git_available(),
            "max_workspaces": self.policy.max_workspaces,
            "default_profile": self.policy.default_profile,
        }

    # --- 内部 ---------------------------------------------------------
    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise errors.WorkspaceError(errors.DISABLED, "工作区功能未开启")

    def _check_capacity(self, conn: Any) -> None:
        if store.count_workspaces(conn) >= self.policy.max_workspaces:
            raise errors.WorkspaceError(
                errors.LIMIT, f"工作区数量已达上限 {self.policy.max_workspaces}"
            )

    def _unique(self, conn: Any, name: str, root_path: str | None) -> None:
        taken = store.find_active_by_name(conn, name)
        if taken is not None:
            raise errors.WorkspaceError(
                errors.NAME_TAKEN, "同名工作区已存在", workspace_id=taken.id
            )
        if root_path is not None:
            occupied = store.find_active_by_root(conn, root_path)
            if occupied is not None:
                raise errors.WorkspaceError(
                    errors.PATH_TAKEN, "该目录已被其他工作区引用", workspace_id=occupied.id
                )

    def _build_workspace(
        self,
        name: str,
        *,
        root_path: str,
        repository: Repository,
        workspace_id: str | None = None,
        default_profile: str | None = None,
    ) -> tuple[Workspace, Repository]:
        now = _now()
        identifier = workspace_id or new_id()
        workspace = Workspace(
            id=identifier,
            name=name,
            owner="local",
            root_path=root_path,
            default_profile=(default_profile or self.policy.default_profile),
            status="active",
            created_at=now,
            updated_at=now,
        )
        bound_repo = Repository(
            workspace_id=identifier,
            source=repository.source,
            managed=repository.managed,
            remote_url=repository.remote_url,
            branch=repository.branch,
            base_commit=repository.base_commit,
            credential_ref=repository.credential_ref,
            detected_git=repository.detected_git,
            created_at=repository.created_at or now,
        )
        return workspace, bound_repo

    @staticmethod
    def _scope_id(workspace_id: str) -> str:
        return f"workspace:{workspace_id}"

    @staticmethod
    def _summary(workspace: Workspace, repository: Repository | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": workspace.id,
            "name": workspace.name,
            "owner": workspace.owner,
            "root_path": workspace.root_path,
            "default_profile": workspace.default_profile,
            "status": workspace.status,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
            "archived_at": workspace.archived_at,
            "source": repository.source if repository else "",
            "managed": repository.managed if repository else False,
            "remote_url": repository.remote_url if repository else "",
            "branch": repository.branch if repository else "",
            "base_commit": repository.base_commit if repository else "",
            "detected_git": repository.detected_git if repository else False,
        }
        return payload


__all__ = ["RepoInfo", "WorkspaceService"]
