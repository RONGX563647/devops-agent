"""评测维度常量 —— correctness / performance / safety 的唯一定义来源。

StrEnum 本身就是 str 子类，可直接与 DB / API 里的字符串比较。
自动打分算法（judge / recall@k）尚未实现，见 Roadmap。
"""

from __future__ import annotations

from enum import StrEnum


class EvalDimension(StrEnum):
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    SAFETY = "safety"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        """判断字符串是否为合法评测维度（反馈入口用它拒绝脏值）。"""
        return value in cls._value2member_map_

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """全部合法维度值，供校验错误信息与文档引用。"""
        return tuple(member.value for member in cls)
