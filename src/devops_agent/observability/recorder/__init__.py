"""事件记录器 —— 消费事件 dict 流，聚合出 run + spans 落库。

职责边界：
- 输入是普通 dict（由 runtime/events.py 转译），完全不 import agentscope；
- 每个 SSE 请求对应一个 Recorder 实例；**连接不持有到对象上** ——
  sqlite3 连接默认绑定创建线程，而「挂起 → 确认」跨了两个请求 / 线程，
  复用长连接会直接抛 `SQLite objects created in a thread...`；
- 用 reply_id 把零散事件配对成 span（START 记起点，END 落一条 span）。

实现按职责拆在 `timing` / `usage` / `recorder`，此文件只做门面 re-export，
对外接口与原单文件模块保持一致（含测试直接引用的时间与统计辅助）。
"""

from __future__ import annotations

from devops_agent.observability.recorder.recorder import MAX_SPAN_TEXT, RunRecorder
from devops_agent.observability.recorder.timing import (
    _elapsed_ms,
    _iso_to_epoch_ms,
    _pending_t0,
    _union_ms,
    utc_now,
)
from devops_agent.observability.recorder.usage import _as_int, _token_delta

__all__ = [
    "MAX_SPAN_TEXT",
    "RunRecorder",
    "_as_int",
    "_elapsed_ms",
    "_iso_to_epoch_ms",
    "_pending_t0",
    "_token_delta",
    "_union_ms",
    "utc_now",
]
