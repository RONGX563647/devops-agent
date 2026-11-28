"""记忆管理 API —— 最小面：状态、会话卡片、长期列表、删除。

只读 SQLite；不 import AgentScope。删除同时覆盖中期版本与长期条目。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from devops_agent.config import load_memory
from devops_agent.memory.context import clip
from devops_agent.memory.runtime import memory_status
from devops_agent.memory.scope import resolve_scope_id
from devops_agent.observability import repository as repo

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _db() -> Any:
    return repo.connect()


def _scope_id(scope: str, session_id: str, workspace_id: str = "") -> str:
    """按 scope 真正分派，规则与运行时同源（`memory.scope.resolve_scope_id`）。

    - session：永远 `session:<id>`；
    - user：配置了 user_id 才共享，否则退回会话；
    - workspace：固定 `workspace:<id>`，跨工作区不可见。
    """
    return resolve_scope_id(load_memory(), session_id, workspace_id, scope=scope)


@router.get("/status")
def get_status() -> dict[str, Any]:
    """三层开关、后端与降级原因。"""
    return memory_status()


@router.get("/session/{session_id}")
def get_session_memory(session_id: str) -> dict[str, Any]:
    """当前 active 卡片 + 版本列表（预览）。"""
    policy = load_memory()
    preview = policy.trace.summary_preview_chars
    with repo.connect() as conn:
        repo.init_db(conn)
        active = repo.active_memory(conn, session_id)
        versions = repo.list_memory_versions(conn, session_id, policy.episodic.max_versions)
    return {
        "session_id": session_id,
        "active": _preview(active, preview),
        "versions": [_preview(item, preview) for item in versions],
    }


def _preview(card: dict[str, Any] | None, limit: int) -> dict[str, Any] | None:
    if card is None:
        return None
    item = dict(card)
    item["text"] = clip(str(item.get("text", "")), limit)
    return item


@router.get("/long-term")
def get_long_term(
    scope: str = Query(default="session", pattern="^(session|user|workspace)$"),
    session_id: str = "",
    workspace_id: str = "",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """长期记忆列表（分页、预览）；默认会话作用域。

    `scope=workspace` 时必须带 `workspace_id`：不猜「当前唯一的工作区」，
    否则一次参数遗漏就会读出别的代码库的记忆。
    """
    if scope == "workspace" and not workspace_id.strip():
        raise HTTPException(status_code=400, detail="workspace_id_required")
    policy = load_memory()
    scope_id = _scope_id(scope, session_id, workspace_id)
    preview = policy.trace.hit_preview_chars
    with repo.connect() as conn:
        repo.init_db(conn)
        items = repo.list_long_term(
            conn, scope=scope, scope_id=scope_id, limit=limit, offset=offset
        )
        total = repo.count_long_term(conn, scope, scope_id)
    return {
        "scope": scope,
        "scope_id": scope_id,
        "items": [{**item, "text": clip(str(item["text"]), preview)} for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/{layer}/{memory_id}")
def delete_memory(layer: str, memory_id: int) -> dict[str, Any]:
    """删除中期版本或长期条目；未知层返回 404。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        if layer in ("episodic", "session"):
            removed = repo.delete_memory_version(conn, memory_id)
        elif layer in ("long", "long_term"):
            removed = repo.delete_long_term(conn, memory_id)
        else:
            raise HTTPException(status_code=404, detail=f"未知记忆层: {layer}")
    return {"layer": layer, "id": memory_id, "deleted": removed}
