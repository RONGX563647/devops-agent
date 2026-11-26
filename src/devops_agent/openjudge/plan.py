"""判分方案（plan.yaml）的加载与指纹：裁判哪些维度、各占多少权重。

拆成独立模块，是因为它被 grade（执行）、report（口径）、
rubric_gate（引用）三处共用，而 grade 本身已经足够长。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PLAN_PATH = Path(__file__).resolve().parents[3] / "eval" / "openjudge" / "plan.yaml"


class ConfigError(RuntimeError):
    """判分环境不完整（缺模型凭据 / 未装 py-openjudge）。"""


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    key: str
    grader: str
    weight: float
    low: float
    high: float
    rubric: str = ""


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    judges: dict[str, JudgeSpec] = field(default_factory=dict)

    @property
    def weights(self) -> dict[str, float]:
        return {key: spec.weight for key, spec in self.judges.items()}


def load_plan(path: Path | None = None) -> Plan:
    """读 plan.yaml；权重和必须为 1（否则「归一化总分」不可比）。"""
    target = path or PLAN_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    judges: dict[str, JudgeSpec] = {}
    for key, spec in (raw.get("judges") or {}).items():
        scale = spec.get("scale") or [0, 1]
        judges[str(key)] = JudgeSpec(
            key=str(key),
            grader=str(spec["grader"]),
            weight=float(spec.get("weight", 0)),
            low=float(scale[0]),
            high=float(scale[1]),
            rubric=str(spec.get("rubric", "")),
        )
    total = sum(spec.weight for spec in judges.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"plan 权重和必须为 1，当前 {total}")
    return Plan(name=str(raw.get("plan", "unnamed")), judges=judges)


def plan_sha(plan: Plan) -> str:
    import hashlib

    blob = json.dumps(
        {key: (s.grader, s.weight, s.low, s.high, s.rubric) for key, s in plan.judges.items()},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
