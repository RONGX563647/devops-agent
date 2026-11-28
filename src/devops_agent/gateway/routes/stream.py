"""POST /run/stream —— 只做 HTTP 契约映射。

三行有效逻辑：档位、占住会话、装响应；顺序与收尾在 turn/orchestrate.py、turn/lease.py。
**出现 `if` 分支或 `try`，就说明逻辑放错了层。**
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from devops_agent.gateway.contract import REQUEST_ID_HEADER, SSE_HEADERS, SSE_MEDIA_TYPE
from devops_agent.gateway.routes.deps import (
    profile_or_403,
    registry_of,
    sandbox_ready,
    settings_of,
)
from devops_agent.gateway.routes.schemas import RunRequest
from devops_agent.gateway.turn.ending import RunLimits
from devops_agent.gateway.turn.lease import open_lease
from devops_agent.gateway.turn.orchestrate import run_turn
from devops_agent.gateway.turn.ports import PendingStore, RuntimePort

router = APIRouter()


@router.post("/run/stream")
async def run_stream(
    req: RunRequest,
    request: Request,
    x_request_id: str | None = Header(default=None, alias=REQUEST_ID_HEADER),
) -> StreamingResponse:
    """SSE 流式端点：实时推送事件，并同步把事件落库为 run + spans。

    1) 档位：未开放的档位直接 403，不降级照跑；
    2) 占住会话：重建实例 / 409 会话忙 / 抢锁，三件事都在返回响应前同步完成；
    3) 交给编排层：帧流由 run.run_turn 生产，这里只负责装进响应。
    """
    # 0) 执行面：required=true 初始化失败时 503，绝不回落宿主执行
    sandbox_ready(request)
    # 1) 档位校验（403 在这里、不是流里）
    profile = profile_or_403(req.permission)
    settings = settings_of(request)
    # 2) 会话租约：返回之后才开始吐流，所以并发错误还能用 4xx 表达
    lease = await open_lease(
        registry_of(request),
        profile,
        req.session_id,
        web_search=req.web_search,
        settings=settings,
        request_id=x_request_id or "",
    )
    # 3) 编排：一轮的业务顺序（结清 → 解析 → 转发 → 收尾）在 turn/orchestrate.py
    return StreamingResponse(
        run_turn(
            lease=lease,
            port=RuntimePort(lease.entry.runtime),
            store=PendingStore(),
            limits=RunLimits.from_policy(settings.stream),
            session_id=req.session_id,
            user_input=req.user_input,
            attachments=req.attachments,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers=dict(SSE_HEADERS),
    )
