"""工作区 HTTP 面（SPEC-0021 §10.1）—— 只做契约映射，业务全在 `WorkspaceService`。

`enabled=false` 时整个路由返回 404：新面默认不存在，回滚只关开关。
本模块不 import `gateway.app`（importlinter: gateway-routes-no-app）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from devops_agent.gateway.routes.workspace_deps import workspace_service_of
from devops_agent.workspace import WorkspaceError, WorkspaceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["workspaces"])

_BASE = "/workspaces"


class LocalSource(BaseModel):
    """本地目录来源。

    `mode` 决定落盘语义：`auto`（默认）在 git 仓库上开工作树、其余原地引用；
    `worktree` 强制开工作树（非 git 目录 400）；`in_place` 强制原地引用。
    """

    kind: Literal["local"]
    path: str
    mode: Literal["auto", "worktree", "in_place"] = "auto"
    branch: str = ""


class GitSource(BaseModel):
    """Git 来源：克隆到受管目录；凭据只给引用名。"""

    kind: Literal["git"]
    url: str
    branch: str = ""
    credential_ref: str = ""
    depth: int = Field(default=1, ge=0, le=100)


class CreateWorkspaceRequest(BaseModel):
    """创建请求：`source.kind` 判别联合，缺字段直接 422。"""

    name: str
    default_profile: str | None = None
    source: LocalSource | GitSource = Field(discriminator="kind")


class UpdateWorkspaceRequest(BaseModel):
    """改名 / 改默认档位。"""

    name: str | None = None
    default_profile: str | None = None


class BindWorkspaceRequest(BaseModel):
    """显式绑定会话到工作区。"""

    workspace_id: str


class UpdateSessionRequest(BaseModel):
    """会话改名 / 归档；两者都可省略，只改传了的那个。"""

    title: str | None = None
    archived: bool | None = None


def _service(request: Request) -> WorkspaceService:
    service = workspace_service_of(request)
    if not service.policy.enabled:
        raise HTTPException(status_code=404, detail="workspace_disabled")
    return service


def _fail(exc: WorkspaceError) -> HTTPException:
    """把领域错误翻成 HTTP。

    默认 `detail` 保持既有契约的「错误码字符串」；只有需要给出结构化细节
    （脏文件列表、占用会话）时才改成对象——那两类信息前端必须能直接展示。
    """
    from devops_agent.gateway.errors import workspace_http  # noqa: PLC0415

    status = workspace_http(exc.code).status_code
    detail: Any = exc.code
    if "changed_files" in exc.extra or "sessions" in exc.extra:
        detail = {"code": exc.code, "message": exc.message, **exc.extra}
    return HTTPException(status_code=status, detail=detail)


@router.get(_BASE)
def list_workspaces(request: Request, status: str | None = None) -> dict[str, Any]:
    """工作区列表（含 source / managed / status）。"""
    service = _service(request)
    try:
        return {"items": service.list(status=status)}
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.post(_BASE)
def create_workspace(req: CreateWorkspaceRequest, request: Request) -> dict[str, Any]:
    """创建工作区：本地目录引用或 Git 导入。"""
    service = _service(request)
    try:
        if isinstance(req.source, LocalSource):
            workspace = service.create_local(
                req.name,
                req.source.path,
                req.default_profile,
                mode=req.source.mode,
                branch=req.source.branch,
            )
        else:
            workspace = service.create_git(
                req.name,
                req.source.url,
                branch=req.source.branch,
                credential_ref=req.source.credential_ref,
                depth=req.source.depth,
                default_profile=req.default_profile,
            )
        return service.detail(workspace.id)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.get(_BASE + "/browse")
def browse_directories(request: Request, path: str = "") -> dict[str, Any]:
    """列出白名单内的子目录（只给名字与路径，不返回文件内容）。"""
    service = _service(request)
    try:
        return service.browse(path)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.get(_BASE + "/control")
def workspace_control(request: Request) -> dict[str, Any]:
    """能力自述：开关、白名单根、git 是否可用、配额。

    必须声明在 `/{workspace_id}` 之前，否则 `control` 会被当成工作区 id 解析。
    前端据此把不可用的来源直接禁用，而不是让用户填完表单才收到 400。
    """
    return _service(request).control()


@router.get(_BASE + "/sessions")
def session_board(
    request: Request,
    limit: int = Query(default=5, ge=1, le=50),
    include_archived: bool = False,
) -> dict[str, Any]:
    """左栏分组视图：每个工作区挂最近 `limit` 条会话，另附「未归属」一栏。

    一次请求给全量分组，是为了让左栏不必按工作区数量发 N 个请求——
    N 个并发请求的到达顺序不定，渲染出的分组会乱跳。
    必须声明在 `/{workspace_id}` 之前，否则 `sessions` 会被当成工作区 id。
    """
    service = _service(request)
    try:
        return service.session_board(limit_per_workspace=limit, include_archived=include_archived)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.get(_BASE + "/sessions/{session_id}")
def session_detail(session_id: str, request: Request) -> dict[str, Any]:
    """单条会话的聚合视图（标题 / 最近时间 / 次数 / 归档状态）。"""
    service = _service(request)
    try:
        return service.session_detail(session_id)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.patch(_BASE + "/sessions/{session_id}")
def update_session(session_id: str, req: UpdateSessionRequest, request: Request) -> dict[str, Any]:
    """给会话改名或归档；两者都不动 runs，也不动工作区归属。

    路径是三段，不会和 `/{workspace_id}` 抢匹配，但仍放在分组路由附近，
    让「会话相关」的端点集中在一处。
    """
    service = _service(request)
    try:
        result: dict[str, Any] = {"session_id": session_id}
        if req.title is not None:
            result.update(service.rename_session(session_id, req.title))
        if req.archived is not None:
            result.update(service.archive_session(session_id, archived=req.archived))
        return result
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.get(_BASE + "/{workspace_id}")
def get_workspace(workspace_id: str, request: Request) -> dict[str, Any]:
    """工作区详情：仓库元信息、绑定会话数、长期记忆条数。"""
    service = _service(request)
    try:
        return service.detail(workspace_id)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.patch(_BASE + "/{workspace_id}")
def update_workspace(
    workspace_id: str, req: UpdateWorkspaceRequest, request: Request
) -> dict[str, Any]:
    """改名 / 改默认档位。"""
    service = _service(request)
    try:
        service.update(workspace_id, name=req.name, default_profile=req.default_profile)
        return service.detail(workspace_id)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.post(_BASE + "/{workspace_id}/archive")
def archive_workspace(workspace_id: str, request: Request) -> dict[str, Any]:
    """归档：拒绝新绑定与新 run，长期记忆保留。"""
    service = _service(request)
    try:
        service.archive(workspace_id)
        return service.detail(workspace_id)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.post(_BASE + "/{workspace_id}/restore")
def restore_workspace(workspace_id: str, request: Request) -> dict[str, Any]:
    """取消归档（P2 的最小实现）。"""
    service = _service(request)
    try:
        service.restore(workspace_id)
        return service.detail(workspace_id)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.delete(_BASE + "/{workspace_id}")
def delete_workspace(
    workspace_id: str, request: Request, force: bool = Query(default=False)
) -> dict[str, Any]:
    """删除：本地引用只注销；受管克隆按脏状态出 patch 或 409。"""
    service = _service(request)
    try:
        return service.delete(workspace_id, force=force)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.get(_BASE + "/{workspace_id}/memory/long-term")
def workspace_long_term(
    workspace_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """固定 workspace scope 的长期记忆列表。"""
    service = _service(request)
    try:
        return service.memory_items(workspace_id, limit=limit, offset=offset)
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.get(_BASE + "/{workspace_id}/sessions")
def workspace_sessions(
    workspace_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_archived: bool = False,
) -> dict[str, Any]:
    """按工作区列会话：最近运行优先，标题取改名后的标题或最早一条输入。"""
    service = _service(request)
    try:
        return service.sessions_for(
            workspace_id, limit=limit, offset=offset, include_archived=include_archived
        )
    except WorkspaceError as exc:
        raise _fail(exc) from None


@router.post("/session/{session_id}/workspace")
def bind_session_workspace(
    session_id: str, req: BindWorkspaceRequest, request: Request
) -> dict[str, Any]:
    """显式绑定会话；已绑到别处返回 409。"""
    service = _service(request)
    try:
        session = service.bind_session(session_id, req.workspace_id)
        return {
            "session_id": session.session_id,
            "workspace_id": session.workspace_id,
            "bound_at": session.bound_at,
            "bound_by": session.bound_by,
        }
    except WorkspaceError as exc:
        raise _fail(exc) from None


__all__ = ["router"]
