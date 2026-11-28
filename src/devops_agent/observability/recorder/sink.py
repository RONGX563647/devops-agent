"""span 落库：起止事件配对后的写入、序号维护与首 token 延迟打点。

从 RunRecorder 里拆出来，是因为「事件分派」和「写一行 span」是两件事：
前者随框架事件族增长，后者只关心 pending 队列、时间口径与库表。
两个职责挤在一个文件里，任何一边加字段都会把文件顶过 500 行上限。
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from devops_agent.observability import repository as repo
from devops_agent.observability.models import SpanRecord
from devops_agent.observability.recorder.timing import (
    _elapsed_ms,
    _pending_t0,
    span_duration,
)

# 失败原因的保留长度：只留首行（聚类按首行归并），再长就是噪声
MAX_ERROR_TEXT = 300


def failure_text(output: str) -> str | None:
    """从失败步骤的返回原文里取首个非空行当原因；取不到返回 None（不编造）。"""
    for line in output.splitlines():
        text = line.strip()
        if text:
            return text[:MAX_ERROR_TEXT]
    return None


class SpanSink:
    """pending 队列 → spans 表：起点偏移、耗时、序号与首 token 都在这里算。

    这些属性由子类在 `__init__` / `attach` 里赋值；这里只声明类型，
    不给默认值，避免「类属性当实例状态」被意外共享。
    """

    _db_path: Path
    _run_id: int | None = None
    _started_at: str = ""
    _offset_base_ms: int = 0
    _t0: int = 0
    _seq: int = 0
    _last_model_span_id: int | None = None
    _pending: dict[str, dict[str, object]]
    _model_pending: dict[str, deque[dict[str, object]]]

    def _rel_ms(self, t_ns: int) -> int:
        """单调时钟 → 相对 run 起点的毫秒偏移。"""
        return self._offset_base_ms + max(0, (t_ns - self._t0) // 1_000_000)

    def _span_offset(self, pending: dict[str, object]) -> int:
        """起点偏移：与耗时同一口径——有事件时间戳就用它，缺失才退回单调时钟。

        两个时钟混用会让「起点偏移」和「耗时」落在两条时间轴上，
        span 并集（work_ms）就再也对不上。
        """
        start_at = str(pending.get("start", "") or "")
        if pending.get("has_time") and start_at:
            return _elapsed_ms(self._started_at, start_at)
        return self._rel_ms(_pending_t0(pending))

    def _event_rel_ms(self, event: dict[str, object]) -> int:
        """事件时间 → 相对 run 起点的毫秒；缺失时退回单调时钟（与 span 同一口径）。"""
        raw_at = str(event.get("created_at", "") or "")
        if raw_at:
            return _elapsed_ms(self._started_at, raw_at)
        return self._rel_ms(time.perf_counter_ns())

    def _open_model(self, reply: str) -> dict[str, object] | None:
        """该 reply 最早一个未闭合的模型起点（不弹出队首）。"""
        queue = self._model_pending.get(reply)
        return queue[0] if queue else None

    def _mark_first_token(self, event: dict[str, object]) -> None:
        """记录本轮模型调用的首 token 延迟（只记第一次增量）。

        思考也算「开始出字」：思考块先到就说明模型已经开始响应，只等正文会把
        TTFT 记成整个生成时长，指标就失去意义了。
        """
        pending = self._open_model(str(event.get("reply_id", "")))
        if pending is None or pending.get("ttft_ms") is not None:
            return
        start = self._span_offset(pending)
        pending["ttft_ms"] = max(0, self._event_rel_ms(event) - start)

    def _pop_model(self, reply: str) -> dict[str, object] | None:
        """按 FIFO 取该 reply 最早一个未闭合的模型起点。"""
        queue = self._model_pending.get(reply)
        if not queue:
            return None
        pending = queue.popleft()
        if not queue:
            self._model_pending.pop(reply, None)
        return pending

    def _close_span(
        self,
        key: str,
        end_at: str,
        end_ns: int,
        tokens: int,
        *,
        name: str | None = None,
        status: str = "ok",
        error: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        """把 pending 中的工具起点配对成一条 span 落库；无起点则忽略。"""
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        self._write_span(
            pending,
            end_at,
            end_ns,
            tokens,
            name=name,
            status=status,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_input_tokens=cache_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        )

    def _write_span(
        self,
        pending: dict[str, object],
        end_at: str,
        end_ns: int,
        tokens: int,
        *,
        name: str | None = None,
        status: str = "ok",
        error: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        """落一条 span：偏移/耗时走单调时钟，工具挂到最近一次模型调用下。"""
        # 1. run 尚未建立时不落库（事件可能早于 start 到达）
        if self._run_id is None:
            return
        # 2. 偏移与耗时同一口径：有事件时间戳优先，缺失才退回单调时钟
        start_at = str(pending.get("start", ""))
        is_tool = str(pending.get("type", "model")) == "tool"
        # 3. seq 全局单调递增，泳道与列表共用同一顺序
        self._seq += 1
        span = SpanRecord(
            id=None,
            run_id=self._run_id,
            type=str(pending.get("type", "model")),
            name=name or str(pending.get("name", "step")),
            start_offset_ms=self._span_offset(pending),
            duration_ms=span_duration(pending, start_at, end_at, end_ns),
            input=str(pending.get("input", "")),
            output=str(pending.get("output", "")),
            tokens=tokens,
            status=status,
            # 失败但没有显式 error 时，从返回原文首行兜一句：空原因等于没法聚类
            error=error
            or (failure_text(str(pending.get("output", ""))) if status == "error" else None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_input_tokens=cache_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            seq=self._seq,
            # 工具 span 挂到发起它的模型调用下：回答得了「哪个模型轮次调了它」
            parent_id=self._last_model_span_id if is_tool else None,
            ttft_ms=int(str(pending.get("ttft_ms") or 0)),
        )
        with repo.connect(self._db_path) as conn:
            span_id = repo.add_span(conn, span)
        # 4. 只有模型 span 更新「最近模型」，工具 span 已按它挂好 parent_id
        if not is_tool:
            self._last_model_span_id = span_id
