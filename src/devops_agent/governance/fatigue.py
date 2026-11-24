"""审批疲劳统计 —— 把「我又被问了」从体感变成一个数字。

开源实现（Claude Code / Gemini CLI / OpenCode）都是被动地「你点一次我记一次」，
不会主动告诉你「这条命令这个月已经被问过 12 次了，该加规则了」。本模块补这一层：
按规则前缀累计被问次数，前端据此提示「一键加规则」。

只放进程内计数：它是提示，不是审计。落库要新增一张表与清理策略，收益不匹配 ——
服务重启后从零累计不影响结论（同一条命令再问两次就会重新冒头）。
"""

from __future__ import annotations

import threading
from collections import Counter

from devops_agent.governance.rules import rule_prefix

# (tool, prefix) → 被问次数
_asks: Counter[tuple[str, str]] = Counter()
_lock = threading.Lock()

# 提示门槛：问过一次可能是偶然，两次以上就值得建议沉淀成规则
FATIGUE_THRESHOLD = 2


def _key(tool: str, command: str) -> tuple[str, str]:
    """按规则前缀归并：`pytest -q` 与 `pytest tests/` 算同一条。"""
    prefix = rule_prefix(command)
    if prefix is not None:
        return (tool, prefix.removesuffix(":*"))
    head = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    return (tool, head)


def record(tool: str, command: str) -> None:
    """记一次「这条命令被问了」。空命令不记，否则会攒出一堆无意义条目。"""
    if not command.strip():
        return
    with _lock:
        _asks[_key(tool, command)] += 1


def snapshot(threshold: int = FATIGUE_THRESHOLD) -> list[dict[str, object]]:
    """被问次数达到门槛的条目，多的在前。"""
    with _lock:
        hits = [
            (tool, f"{prefix}:*" if prefix else "", count)
            for (tool, prefix), count in _asks.items()
            if count >= threshold
        ]
    # 先按次数排再转成 dict：直接对 dict 取 int() 会把类型检查逼进 object
    hits.sort(key=lambda row: row[2], reverse=True)
    return [{"tool": tool, "prefix": prefix, "count": count} for tool, prefix, count in hits]


def reset() -> None:
    """清空计数（测试用；也可以给「我不想知道」的部署当开关）。"""
    with _lock:
        _asks.clear()
