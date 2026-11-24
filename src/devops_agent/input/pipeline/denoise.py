"""去噪与截断：移除 base64 块、折叠连续重复行、中段截断。"""

from __future__ import annotations

import re
from typing import Final

_BASE64_RE: Final = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


def _dedupe_lines(text: str) -> tuple[str, int]:
    """折叠连续重复行（日志里最常见的噪声），返回 (文本, 折叠行数)。"""
    out: list[str] = []
    removed = 0
    previous: str | None = None
    repeat = 0
    for line in text.split("\n"):
        if line and line == previous:
            repeat += 1
            removed += 1
            continue
        if repeat:
            out.append(f"…（重复 {repeat} 行）")
            repeat = 0
        out.append(line)
        previous = line
    if repeat:
        out.append(f"…（重复 {repeat} 行）")
    return "\n".join(out), removed


def _denoise(text: str) -> tuple[str, list[str]]:
    """去噪：移除 base64 块、折叠连续重复行。"""
    notes: list[str] = []
    work = text
    b64 = _BASE64_RE.findall(work)
    if b64:
        work = _BASE64_RE.sub("[已移除 base64 块]", work)
        notes.append(f"移除 base64 块 ×{len(b64)}")
    work, removed = _dedupe_lines(work)
    if removed:
        notes.append(f"折叠重复行 ×{removed}")
    return work, notes


def _truncate(text: str, limit: int) -> str:
    """中段截断：保留首尾，中间留一行说明，避免只剩开头看不出结论。"""
    if len(text) <= limit:
        return text
    marker = f"\n\n…（已截断 {len(text) - limit} 字）…\n\n"
    budget = max(0, limit - len(marker))
    head = int(budget * 0.6)
    tail = budget - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"
