"""脱敏规则的公共入口（叶子模块，详见 `rules` 的设计说明）。"""

from __future__ import annotations

from .rules import (
    Applied,
    Candidate,
    find_candidates,
    identifiers_free,
    redact,
    shannon_entropy,
    shape_ok,
)

__all__ = [
    "Applied",
    "Candidate",
    "find_candidates",
    "identifiers_free",
    "redact",
    "shape_ok",
    "shannon_entropy",
]
