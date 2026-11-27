"""时间工具 —— ISO 时间解析、区间并集与单调时钟换算。"""

from __future__ import annotations

from datetime import UTC, datetime

from devops_agent.observability.models import SpanRecord


def utc_now() -> str:
    """当前 UTC 时间（ISO 8601），供 recorder 打点。"""
    return datetime.now(UTC).isoformat()


def _elapsed_ms(start: str, end: str) -> int:
    """两个 ISO 时间串的毫秒差；解析失败或时区不一致回退 0，不让埋点拖垮主流程。"""
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        return max(0, int(delta.total_seconds() * 1000))
    except (ValueError, TypeError):  # TypeError：offset-naive 与 offset-aware 混减
        return 0


def _union_ms(intervals: list[tuple[int, int]]) -> int:
    """多个区间的并集长度。

    模型调用与工具调用可能嵌套（思考发生在模型调用内部、并行工具互相重叠），
    直接求和会把同一段时间算两次；并集才是「真正在干活」的时间。
    """
    total = 0
    cur_start: int | None = None
    cur_end = 0
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if cur_start is None:
            cur_start, cur_end = start, end
        elif start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    if cur_start is not None:
        total += cur_end - cur_start
    return total


def _iso_to_epoch_ms(value: str) -> int:
    """ISO 时间 → epoch 毫秒；解析失败退化为当前时间，埋点不该拖垮主流程。"""
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except (ValueError, TypeError):
        return int(datetime.now(UTC).timestamp() * 1000)


def _pending_t0(pending: dict[str, object]) -> int:
    """取 pending 里记下的单调时钟起点（纳秒）；缺省回 0。"""
    value = pending.get("t0", 0)
    return value if isinstance(value, int) else 0


def _work_ms(spans: list[SpanRecord], wall_ms: int) -> int:
    """实测耗时 = 所有 span 区间的并集，且不超过墙钟。

    实测不可能超过墙钟（事件时钟偶尔会比 run 起点更早），超了就以墙钟封顶。
    """
    return min(
        _union_ms(
            [
                (span.start_offset_ms, span.start_offset_ms + (span.duration_ms or 0))
                for span in spans
            ]
        ),
        wall_ms,
    )


def span_duration(pending: dict[str, object], start_at: str, end_at: str, end_ns: int) -> int:
    """优先用事件时间戳（真实发生时刻），缺失时退回单调时钟差值。

    created_at 缺失时旧实现把起止都填成 utc_now()，于是每条 span 都是 0ms；
    单调时钟不依赖事件字段，才画得出真实宽度。
    """
    if pending.get("has_time") and end_at:
        measured = _elapsed_ms(start_at, end_at)
        if measured > 0:
            return measured
    t0 = _pending_t0(pending)
    return max(0, (end_ns - t0) // 1_000_000)
