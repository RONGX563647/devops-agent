"""健康分的数据模型与常量 —— 供各维度计算与聚合共用。"""

from __future__ import annotations

from dataclasses import dataclass, field

# 评分口径版本：算法或默认配置变化时递增，便于解释「历史分数为什么不可直接比」
SCORING_VERSION = "v2"

# 评分维度 key（也是对外输出的子分名称）
DIMENSIONS = ("execution", "efficiency", "quality", "safety")

# 维度中文名：前端与告警文案共用一份，避免各处自己翻译
DIMENSION_LABELS: dict[str, str] = {
    "execution": "执行健康",
    "efficiency": "效率成本",
    "quality": "效果质量",
    "safety": "安全合规",
}


def _clamp(value: float) -> float:
    """限制到 0-100。"""
    return max(0.0, min(100.0, value))


def _round(value: float, digits: int = 1) -> float:
    """统一保留位数：同一份数据重算必须得到同一个数。"""
    return round(value, digits)


@dataclass(frozen=True, slots=True)
class ScoreItem:
    """一条扣分/加分项：回答「这 12 分扣在哪」。"""

    label: str
    delta: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DimensionScore:
    """单个维度的得分与解释。value 为 None 表示证据不足、不参与聚合。"""

    key: str
    label: str
    value: float | None
    weight: float
    confidence: float
    note: str
    items: tuple[ScoreItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """一次运行的完整评分报告。"""

    score: float
    grade: str
    coverage: float
    confidence: float
    version: str
    dimensions: dict[str, DimensionScore]

    def as_dict(self) -> dict[str, object]:
        """转成 JSON 友好的结构（API 与前端共用）。"""
        return {
            "score": self.score,
            "grade": self.grade,
            "coverage": self.coverage,
            "confidence": self.confidence,
            "version": self.version,
            "dimensions": {
                key: {
                    "key": dim.key,
                    "label": dim.label,
                    "value": dim.value,
                    "weight": dim.weight,
                    "confidence": dim.confidence,
                    "note": dim.note,
                    "items": [
                        {"label": item.label, "delta": item.delta, "detail": item.detail}
                        for item in dim.items
                    ],
                }
                for key, dim in self.dimensions.items()
            },
        }
