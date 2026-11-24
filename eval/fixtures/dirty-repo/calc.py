"""dirty-repo 的模块：一个能过、一个有 bug。"""

from __future__ import annotations

import os  # noqa: F401  # 故意保留未使用的 import，供「诊断 lint 违规」用例命中


def add(left: int, right: int) -> int:
    """返回两数之和。"""
    return left + right


def average(values: list[int]) -> float:
    """返回平均值。空列表时返回 0.0。

    BUG（故意保留）：这里没有处理空列表，会抛 ZeroDivisionError。
    """
    return sum(values) / len(values)
