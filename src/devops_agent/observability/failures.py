"""失败原因聚类：run 级异常与逐次调用的报错放在同一个榜里。

只聚 `runs.error` 时，工具报错（`Command failed` / `Permission denied`）
一条都进不来，看板上就只剩一行「未记录失败原因」——原因记了却没被读。
"""

from __future__ import annotations

from collections import Counter

from devops_agent.observability.models import RunRecord

# 原因原文保留长度：够看清是哪类错，又不会把整段堆栈顶到界面上
MAX_REASON_TEXT = 120


def reason_line(raw: str, fallback: str) -> str:
    """原因原文压成一行：首个非空行，超长截断；全空则用 fallback。"""
    for line in raw.strip().splitlines():
        text = line.strip()
        if text:
            return text[:MAX_REASON_TEXT]
    return fallback


def failure_clusters(
    runs: list[RunRecord],
    spans: list[dict[str, str]],
    top: int = 8,
) -> list[dict[str, object]]:
    """把失败原因聚类：同一个报错反复出现才值得先修。

    每条失败都占一次计数（不是每轮一次）：一轮里失败三次同一句报错，
    就说明它在该轮反复发生，这比「有一轮失败过」更有信息量。
    """
    counter: Counter[tuple[str, str, str]] = Counter()
    for run in runs:
        if run.status == "success":
            continue
        reason = reason_line(run.error or "", "(未记录失败原因)")
        counter[(reason, "run", "")] += 1
    for span in spans:
        reason = reason_line(span.get("error", ""), "(未记录失败原因)")
        counter[(reason, span.get("type", ""), span.get("name", ""))] += 1
    return [
        {"reason": reason, "count": count, "kind": kind, "name": name}
        for (reason, kind, name), count in counter.most_common(top)
    ]
