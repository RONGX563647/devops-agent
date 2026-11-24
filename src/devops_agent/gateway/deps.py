"""请求级依赖：档位解析、会话注册表与运行记录器。"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from devops_agent.governance.profiles import PermissionProfile, UnknownProfile, resolve
from devops_agent.observability.recorder import RunRecorder
from devops_agent.runtime.sessions import SessionEntry, SessionRegistry, registry_for


def _profile_or_403(name: str | None) -> PermissionProfile:
    """解析请求里的档位；未开放/不存在一律 403，不静默降级。

    静默降级到默认档会让「我明明选了放开档」变成「怎么还是不行」，更难排查。
    """
    try:
        return resolve(name)
    except UnknownProfile:
        raise HTTPException(status_code=403, detail="permission_not_available") from None


def _registry(request: Request) -> SessionRegistry:
    """取会话注册表（实现见 runtime/sessions.registry_for，模型配置端点共用）。"""
    return registry_for(request)


def _recorder_for(entry: SessionEntry, *, profile_key: str, request_id: str = "") -> RunRecorder:
    """建一次运行的记录器，并把本轮实际下发的参数快照一起带上。

    参数不进 run 记录，一旦可配就不可归因：健康分与评测批次会把
    「参数变化点前后」混在一起算。取不到（Echo）时留空。
    """
    params_of = getattr(entry.runtime, "model_params_record", None)
    record = params_of() if callable(params_of) else None
    return RunRecorder(
        domain=os.environ.get("DEVOPS_AGENT_DOMAIN", "debug"),
        model=os.environ.get("DEVOPS_AGENT_MODEL", ""),
        request_id=request_id,
        permission=profile_key,
        model_params=record or {},
    )
