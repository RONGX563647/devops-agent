"""会话级权限端点 —— 本部署开放的档位与运行中切档。

两个只读/单写接口，判断逻辑都在 governance.presets：本模块只做契约映射。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from devops_agent.gateway.deps import profile_or_403, registry_of
from devops_agent.gateway.errors import ErrorCode, http_error
from devops_agent.gateway.schemas import SessionPermissionRequest
from devops_agent.governance.presets import available, default_profile

router = APIRouter()


@router.get("/api/permissions")
def list_permissions() -> dict[str, Any]:
    """本部署开放的档位。

    `write` 由档位自己声明（`PermissionProfile.can_write`），不再靠
    「工具名里有没有 write 这个子串」来猜 —— 那种探测在工具改名时会静默失真。
    """
    return {
        "default": default_profile().key,
        "available": [
            {
                "key": item.key,
                "label": item.label,
                "note": item.note,
                "fallback": item.fallback,
                "write": item.can_write,
            }
            for item in available()
        ],
    }


@router.post("/api/session/{session_id}/permission")
def switch_session_permission(
    session_id: str, req: SessionPermissionRequest, request: Request
) -> dict[str, Any]:
    """运行中切档：原地热替换当前会话的运行时，不打断正在跑的 SSE 流。

    1) 路径与 body 的 session_id 必须一致（否则说明调用方拼错了请求）；
    2) 档位仍然要过白名单，不能靠这个端点提权；
    3) `registry.switch` 能热替换就原地改，并把快照里的档位一并更新；
    4) 如实回报生效范围 —— `applied=False` 时不能说「立即生效」。
    """
    # 1) 自相矛盾的请求直接拒，不要挑一个「看起来对」的用
    if req.session_id != session_id:
        raise http_error(ErrorCode.REQUEST_MISMATCH)
    # 2) 白名单校验与发起轮一致
    profile = profile_or_403(req.permission)
    # 3) 热替换（无活跃实例时按新档建一份给后续轮次用）
    _entry, applied = registry_of(request).switch(session_id, profile, req.web_search)
    # 4) 文案与码双轨：前端可以用 code 出文案，过渡期仍能读 message
    return {
        "session_id": session_id,
        "permission": profile.key,
        "applied": applied,
        "effective_from": "下一次工具判定与下一次模型调用" if applied else "下一轮",
        "effective_from_code": "applied" if applied else "next_turn",
    }
