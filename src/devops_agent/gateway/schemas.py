"""网关请求体模型：只描述 HTTP 契约，不含业务逻辑。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    session_id: str
    user_input: str
    # 附件 id 列表（由 POST /api/attachments 返回）：正文留服务端，只在这里带 id
    attachments: list[str] = Field(default_factory=list)
    # 权限档位；省略用服务端默认档
    permission: str | None = None
    # 逐请求的联网搜索开关：省略 = 按部署默认（DEVOPS_AGENT_ENABLE_SEARCH），
    # 显式 true/false = 这一轮就按它来。默认值不能写 False ——
    # 那样「没带字段」会被当成「关」，环境变量就永远不生效。
    web_search: bool | None = None


class ConfirmItem(BaseModel):
    """前端回的确认结果：只给 id 与决策，命令原文以服务端挂起态为准。"""

    tool_call_id: str
    confirmed: bool
    # once / exact / prefix，非法值在 parse_decisions 里降级为 once
    scope: str = "once"


class ConfirmRequest(BaseModel):
    session_id: str
    results: list[ConfirmItem] = Field(default_factory=list)
    # 用户在确认卡片上切过档时带当前档位；确认续跑按它执行，不再锁死发起轮。
    permission: str | None = None


class SessionPermissionRequest(BaseModel):
    """会话中切档的请求体；session_id 在路径与 body 各留一份，服务端校验一致。"""

    session_id: str
    permission: str
    web_search: bool | None = None
