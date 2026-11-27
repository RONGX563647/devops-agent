"""Token 估算 —— 记忆与上下文预算共用的唯一口径。

刻意不引入 tokenizer 依赖：这里只用于「分区预算」的量级判断，
真正的压缩阈值仍由框架按模型计数决定。
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """粗略估 token：中文约 1.4 字/token，ASCII/JSON 约 3.6 字符/token。

    系数按实测校准：409 字系统提示 + 10139 字符工具 schema ≈ 3060 token。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk / 1.4 + (len(text) - cjk) / 3.6)


def clip_to_tokens(text: str, max_tokens: int) -> str:
    """按 token 预算截断文本；max_tokens<=0 时返回空串。

    估算函数随截断长度单调不减，因此用二分找最长可行前缀，
    不为了一个省略号再超预算。
    """
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    return text[:low]
