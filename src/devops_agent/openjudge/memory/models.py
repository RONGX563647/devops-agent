"""记忆评测的数据模型（SPEC-0014 §4/§5）。

只放纯数据结构与常量，不 import 框架、不碰数据库：
suite 解析、harness、metrics、gates 都依赖这里，任何 IO 都留在各自模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 六条记忆赛道；与 openjudge 的 A–E 是两套独立口径，不能混表。
TRACKS: tuple[str, ...] = ("MT", "MS", "CP", "RT", "NB", "PR")
TIERS: tuple[str, ...] = ("must_pass", "regression", "exploratory")
DEFAULT_TIER = "must_pass"


@dataclass(frozen=True, slots=True)
class Turn:
    """一轮用户输入；`remember` 表示按写入策略应落长期，「junk」仅用于统计。"""

    text: str
    remember: bool = True
    junk: bool = False


@dataclass(frozen=True, slots=True)
class Session:
    """一个会话脚本；同一 case 内的会话共享长期 scope。"""

    id: str
    turns: tuple[Turn, ...]


@dataclass(frozen=True, slots=True)
class Probe:
    """探针：`recall`/`constraint` 要求注入含全部要点，`isolation` 要求不含。"""

    after: str
    question: str
    kind: str = "recall"
    expected_contains: tuple[str, ...] = ()
    expected_absent: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Compression:
    """强制压缩的脚本：摘要由用例给出，保证确定性。"""

    after: str
    summary: str
    before_tokens: int
    after_tokens: int
    threshold: int
    artifacts: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryCase:
    """一条记忆用例（不可变，便于并发与哈希）。"""

    id: str
    track: str
    tier: str
    memory_required: bool
    tags: tuple[str, ...]
    sessions: tuple[Session, ...]
    probes: tuple[Probe, ...]
    privacy: tuple[str, ...] = ()
    compression: Compression | None = None
    #: 检索指标用的 gold 文本片段（事实 token）。
    gold: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """变体 = 配置差量；`overrides` 只描述关闭/调整了哪几层。"""

    name: str
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryPlan:
    """`eval/memory/plan.yaml` 的类型化视图。"""

    name: str
    suite_dir: Path
    baseline: str
    primary: str
    variants: dict[str, VariantSpec]
    trials: int
    min_n: int
    thresholds: dict[str, float]
    require_ci: bool
    alpha: float
    cost_enforced: bool
    red_lines: tuple[str, ...]
    max_tokens: int
    max_minutes: int
