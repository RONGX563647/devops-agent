"""POST /run/stream —— 只做 HTTP 契约映射。

这个文件刻意保持薄：三行有效逻辑（档位、占住会话、装响应），
剩下的顺序与收尾都在 run.py / lease.py。判断它是否走样的标准很简单 ——
**如果这里出现了 `if` 分支或 `try`，说明有逻辑放错了层。**

为什么 409 必须在返回响应之前给出：一旦开始吐流，状态码已经固定成 200，
「会话忙」就只能表达成流里的一条错误帧，前端没法据此重试。
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from devops_agent.gateway.contract import REQUEST_ID_HEADER, SSE_HEADERS, SSE_MEDIA_TYPE
from devops_agent.gateway.deps import profile_or_403, registry_of, settings_of
from devops_agent.gateway.ending import RunLimits
from devops_agent.gateway.lease import open_lease
from devops_agent.gateway.ports import PendingStore, RuntimePort
from devops_agent.gateway.run import run_turn
from devops_agent.gateway.schemas import RunRequest

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
    # 3) 编排：一轮的业务顺序（结清 → 解析 → 转发 → 收尾）在 run.py
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
