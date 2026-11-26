"""线协议契约：SSE 帧、传输常量、挂起载荷。

叶子模块（`.importlinter` 强制）：只被 import，不 import 兄弟模块 —— 改帧字段的人
不必读会话锁与编排顺序。三条不变的约定：

1. 帧只有两种 event 名（done / parked），其余靠 data 里的 `type` 分派；
2. `RUN_DONE` 必须同时给 input 与两个 cache 字段：只给 input 时，前端拿最后一次
   命中除以整轮输入，命中率会被低估「调用次数」倍；
3. 挂起载荷的写（park）与读（GET pending）共用 `PendingPayload` —— 形状一旦分叉，
   刷新后就重建不出确认卡片。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from devops_agent.observability.models import RunStatus

# --- 传输常量 ------------------------------------------------------------

SSE_MEDIA_TYPE: Final = "text/event-stream"
# 幂等键：重试同一请求不会写入重复 run（runs.request_id 有唯一索引兜底）
REQUEST_ID_HEADER: Final = "X-Request-ID"
# 三个响应头缺一不可：no-cache 防中间层缓存、keep-alive 保长连接、
# X-Accel-Buffering 关掉 nginx 缓冲（否则整段流攒到结束才下发）
SSE_HEADERS: Final[Mapping[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
# 心跳帧（SSE 注释行）：模型首 token 前先有字节，别让代理把空窗当空闲连接
PING_FRAME: Final = ": ping\n\n"

# 带 event 名的帧：前端按它切换消息状态（done=完成 / parked=等待确认）
EVENT_DONE: Final = "done"
EVENT_PARKED: Final = "parked"

# data 帧的 type 字段（前端 handlers 按它分派）
FRAME_RUN_DONE: Final = "RUN_DONE"
FRAME_RUN_PARKED: Final = "RUN_PARKED"
FRAME_RUN_ERROR: Final = "RUN_ERROR"

# 挂起载荷的字段名：写入方与读取方共用，改这里两侧一起改
PENDING_TOOL_CALLS: Final = "tool_calls"
PENDING_EXPIRES_AT: Final = "expires_at"

# GET /run/{run_id}/pending 响应里的状态标记（前端按它渲染确认卡片）
PENDING_STATUS: Final = "parked"


# --- 帧编码 --------------------------------------------------------------


def sse_frame(payload: Mapping[str, Any]) -> str:
    """把一个 dict 包装成标准 SSE `data:` 帧（前端按 type 字段分派）。"""
    return f"data: {json.dumps(dict(payload), ensure_ascii=False)}\n\n"


def named_frame(name: str, payload: Mapping[str, Any]) -> str:
    """带 event 名的 SSE 帧（只有 done / parked 用）。"""
    return f"event: {name}\ndata: {json.dumps(dict(payload), ensure_ascii=False)}\n\n"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一轮 run 的用量汇总（全程累加值，不是最后一次调用）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def done_frame(*, run_id: int | None, text: str, usage: TokenUsage) -> str:
    """正常结束帧：run_id、用量汇总与最终文本（用量必须成组给，见模块约定 2）。"""
    return named_frame(
        EVENT_DONE,
        {
            "type": FRAME_RUN_DONE,
            "run_id": run_id,
            "status": RunStatus.SUCCESS.value,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_input_tokens": usage.cache_input_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "text": text,
        },
    )


def parked_frame(
    *,
    run_id: int | None,
    session_id: str,
    expires_at: str,
    tool_calls: Sequence[Mapping[str, Any]],
) -> str:
    """挂起帧：流正常结束但不发 done，前端据此标「等待确认」。"""
    return named_frame(
        EVENT_PARKED,
        {
            "type": FRAME_RUN_PARKED,
            "run_id": run_id,
            "session_id": session_id,
            "expires_at": expires_at,
            "tool_calls": [dict(item) for item in tool_calls],
        },
    )


def error_frame(message: str) -> str:
    """失败帧：`run._guarded` 捕获的异常在这里转成前端可读的一条。"""
    return sse_frame({"type": FRAME_RUN_ERROR, "error": message})


# --- 挂起载荷 ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingPayload:
    """挂起载荷：写入 `runs.pending_json`，读侧必须容忍脏数据。

    空载荷优于抛异常 —— 一张空卡片至少能让人重新发一条消息。
    """

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    expires_at: str = ""

    def to_json(self) -> str:
        """序列化成列值（ensure_ascii=False 让库里直接可读中文命令）。"""
        return json.dumps(
            {PENDING_TOOL_CALLS: self.tool_calls, PENDING_EXPIRES_AT: self.expires_at},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> PendingPayload:
        """从列值还原；解析失败按空载荷处理（不阻断挂起态查询）。"""
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        calls = data.get(PENDING_TOOL_CALLS)
        return cls(
            tool_calls=[dict(item) for item in calls] if isinstance(calls, list) else [],
            expires_at=str(data.get(PENDING_EXPIRES_AT) or ""),
        )
