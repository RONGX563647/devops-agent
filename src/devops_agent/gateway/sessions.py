"""会话级权限端点：本部署开放的档位与运行中切档。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from devops_agent.gateway.deps import _profile_or_403, _registry
from devops_agent.gateway.schemas import SessionPermissionRequest
from devops_agent.governance.profiles import available, default_profile

router = APIRouter()


@router.get("/api/permissions")
def list_permissions() -> dict[str, Any]:
    """本部署开放的档位。默认档可能不在可用集里（配置写错），此时回落到首项。"""
    return {
        "default": default_profile().key,
        "available": [
            {
                "key": item.key,
                "label": item.label,
                "note": item.note,
                "fallback": item.fallback,
                "write": "write" in item.tools,
            }
            for item in available()
        ],
    }


@router.post("/api/session/{session_id}/permission")
def switch_session_permission(
    session_id: str, req: SessionPermissionRequest, request: Request
) -> dict[str, Any]:
    """运行中切档：原地热替换当前会话的运行时，不打断正在跑的 SSE 流。

    白名单校验与发起轮一致（未开放档仍然 403），不能靠这个端点提权。
    `applied` 如实区分「已热替换」与「没有活跃实例、下一轮才装配」，
    免得界面把后者也说成立即生效。
    """
    if req.session_id != session_id:
        raise HTTPException(status_code=400, detail="session_mismatch")
    profile = _profile_or_403(req.permission)
    registry = _registry(request)
    _entry, applied = registry.switch(session_id, profile, req.web_search)
    return {
        "session_id": session_id,
        "permission": profile.key,
        "applied": applied,
        "effective_from": "下一次工具判定与下一次模型调用" if applied else "下一轮",
    }
