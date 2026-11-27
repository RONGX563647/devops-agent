"""Token 估算 —— 记忆与上下文预算共用的唯一口径。

刻意不引入 tokenizer 依赖：这里只用于「分区预算」的量级判断，
真正的压缩阈值仍由框架按模型计数决定。

系数只在这里定义一次；校准样本在 `tests/fixtures/token_calibration.json`，
由 `tests/unit/test_memory_tokens_calibration.py` 断言误差带并检测提示词漂移。
改系数必须同时改 fixture，否则那条用例会红——这正是旧版本缺的一环：
docstring 里写着「409 字 + 10139 字符 ≈ 3060 token」，而真实提示词已经长大，
没人发现。
"""

from __future__ import annotations

import json
from typing import Any

# 中文与 ASCII 的单位 token 字符数，按线上单次调用反推（见 calibration fixture）
CJK_CHARS_PER_TOKEN = 1.4
ASCII_CHARS_PER_TOKEN = 3.6


def estimate_tokens(text: str) -> int:
    """粗略估 token：中文约 1.4 字/token，ASCII/JSON 约 3.6 字符/token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk / CJK_CHARS_PER_TOKEN + (len(text) - cjk) / ASCII_CHARS_PER_TOKEN)


def block_text(block: Any) -> str:
    """任意内容块 → 文本：工具结果取 `output`，文本块取 `text`。

    这里曾经只读 `.text`，而 `ToolResultBlock` 的字段名是 `output` —— 于是全部
    工具结果在估算里算 0 token。后果不只是「估算偏低」：以估算为判据的预算早退
    （SPEC-0019 §3 S0）会永远成立，等于把裁剪整条功能关掉。
    """
    output = getattr(block, "output", None)
    if isinstance(output, str):
        return output
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    if output is not None:
        return json.dumps(output, ensure_ascii=False, default=str)
    return ""


def content_text(content: Any) -> str:
    """消息内容 → 文本：字符串原样，块列表逐块取文本后拼接。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(part for part in (block_text(block) for block in content) if part)


def estimate_messages(messages: list[Any]) -> int:
    """整段历史的估算 token（含工具结果）：预算判定的唯一口径。"""
    return sum(estimate_tokens(content_text(getattr(msg, "content", ""))) for msg in messages)
