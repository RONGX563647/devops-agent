"""统计口径：区间估计与两比例检验。

为什么单独一个文件：`1/n` 不是精度上限，只是一个用例的权重。n=10 时它给出 10pp，
n=100 时给出 1pp，看上去「样本变大就更精确」，但它不随观测到的通过率变化——
10 条全过与 10 条六过被当成同样的不确定性。Wilson 区间在极端比例下不会塌成
一个点，才是能拿去做「这次是不是真变差」判断的量。
"""

from __future__ import annotations

import math

#: 正态分位数 1.96 ≈ 95% 双侧
Z95 = 1.959963984540054


def wilson_interval(passed: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """二项比例的 Wilson 得分区间；n=0 时没有区间可言，返回 None。"""
    if n <= 0:
        return None
    phat = min(1.0, max(0.0, passed / n))
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def two_proportion_p(p1: float, n1: int, p2: float, n2: int) -> float | None:
    """两比例 z 检验的 p 值（合并方差）；任一组为空或方差为 0 时返回 None。

    返回 None 表示「无法判断」，不是「没有差异」——调用方必须把这两种情况分开显示。
    """
    if n1 <= 0 or n2 <= 0:
        return None
    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    variance = pooled * (1 - pooled) * (1 / n1 + 1 / n2)
    if variance <= 0:
        return None
    z = abs(p1 - p2) / math.sqrt(variance)
    return max(0.0, min(1.0, 2 * (1 - _normal_cdf(z))))


def is_significant(p1: float, n1: int, p2: float, n2: int, alpha: float = 0.05) -> bool | None:
    """p < alpha 即显著；样本不足时返回 None（不是 False）。"""
    p_value = two_proportion_p(p1, n1, p2, n2)
    if p_value is None:
        return None
    return p_value < alpha
