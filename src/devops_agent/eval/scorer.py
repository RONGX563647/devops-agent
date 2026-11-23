"""评测：三维打分（正确性/性能/安全）。"""

from __future__ import annotations

from enum import StrEnum


class EvalDimension(StrEnum):
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    SAFETY = "safety"
