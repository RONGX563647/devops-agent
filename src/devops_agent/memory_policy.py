"""记忆策略类型（memory.yaml 的类型化视图）。

单独成文件是为了让 config.py 保持在 500 行以内；这里只依赖 pydantic，
不反向依赖 config，因此 config 可以安全地 load 并在命名空间里再导出。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ShortTermPolicy(BaseModel):
    """短期（工作记忆）的确定性裁剪旋钮。"""

    max_turns: int = Field(default=12, ge=1, le=200)
    tool_output_chars: int = Field(default=1600, ge=80, le=200_000)
    tool_output_head_ratio: float = Field(default=0.7, gt=0, lt=1)
    dedup_reads: bool = True


class EpisodicPolicy(BaseModel):
    """中期（会话情景）的卡片与版本策略。"""

    enabled: bool = True
    trigger_turns: int = Field(default=6, ge=1, le=200)
    max_chars: int = Field(default=3000, ge=200, le=200_000)
    max_versions: int = Field(default=20, ge=1, le=500)
    ttl_days: int = Field(default=30, ge=1, le=3650)


class LongTermPolicy(BaseModel):
    """长期（语义 + 程序性）的后端与检索策略。"""

    enabled: bool = True
    backend: Literal["auto", "mem0", "agentic", "local", "off"] = "auto"
    scope: Literal["session", "user"] = "session"
    top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.35, ge=0, le=1)
    max_entries: int = Field(default=200, ge=1, le=10_000)
    ttl_days: int = Field(default=90, ge=1, le=3650)
    write_on: Literal["turn", "session_end"] = "turn"


class MemoryBudgetPolicy(BaseModel):
    """三层在「历史面额度」里的分账比例；比例之和必须为 1。"""

    short_term_ratio: float = Field(default=0.50, ge=0, le=1)
    episodic_ratio: float = Field(default=0.30, ge=0, le=1)
    long_term_ratio: float = Field(default=0.10, ge=0, le=1)
    headroom_ratio: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def _check_sum(self) -> MemoryBudgetPolicy:
        total = (
            self.short_term_ratio + self.episodic_ratio + self.long_term_ratio + self.headroom_ratio
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError("memory.budget 四项比例之和必须为 1")
        return self


class MemoryContextPolicy(BaseModel):
    """上下文装配旋钮：尾部注入与快照体积。"""

    enabled: bool = True
    note_max_chars: int = Field(default=1200, ge=0, le=200_000)
    episodic_note_ratio: float = Field(default=0.6, ge=0, le=1)
    snapshot_max_chars: int = Field(default=1_500_000, ge=10_000, le=100_000_000)
    snapshot_version: int = Field(default=2, ge=1, le=100)
    assemble_event: bool = True


class MemoryTracePolicy(BaseModel):
    """轨迹预览长度：只放预览，不放全文。"""

    query_preview_chars: int = Field(default=120, ge=10, le=2000)
    hit_preview_chars: int = Field(default=80, ge=10, le=2000)
    summary_preview_chars: int = Field(default=160, ge=10, le=4000)


class MemoryPolicy(BaseModel):
    """memory.yaml 的完整类型化视图（三层 + 预算 + 上下文 + 轨迹）。"""

    enabled: bool = True
    user_id: str = ""
    short_term: ShortTermPolicy = Field(default_factory=ShortTermPolicy)
    episodic: EpisodicPolicy = Field(default_factory=EpisodicPolicy)
    long_term: LongTermPolicy = Field(default_factory=LongTermPolicy)
    budget: MemoryBudgetPolicy = Field(default_factory=MemoryBudgetPolicy)
    context: MemoryContextPolicy = Field(default_factory=MemoryContextPolicy)
    compress: dict[str, bool] = Field(default_factory=lambda: {"fail_open": True})
    trace: MemoryTracePolicy = Field(default_factory=MemoryTracePolicy)


__all__ = [
    "EpisodicPolicy",
    "LongTermPolicy",
    "MemoryBudgetPolicy",
    "MemoryContextPolicy",
    "MemoryPolicy",
    "MemoryTracePolicy",
    "ShortTermPolicy",
]
