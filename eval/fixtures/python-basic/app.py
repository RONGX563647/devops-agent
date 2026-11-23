"""python-basic 的业务模块。"""

from __future__ import annotations


def add(left: int, right: int) -> int:
    """返回两个整数之和。"""
    return left + right


def divide(numerator: float, denominator: float) -> float:
    """返回商；除数为 0 时抛出 ZeroDivisionError。"""
    return numerator / denominator
