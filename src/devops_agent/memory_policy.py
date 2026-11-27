"""记忆策略类型（三层记忆的配置模型）。

单独成文件是为了让 config.py 保持在 500 行以内；这里只依赖 pydantic，
不反向依赖 config，因此 config 可以安全地 load 并在命名空间里再导出。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ShortTermPolicy(BaseModel):
    """短期（工作记忆）的确定性裁剪旋钮。

    `tool_output_chars` 只管**已消化**的旧轮次；尾部 `recent_exempt_results` 个
    工具结果完整保留，它们正是下一步推理的依据（SPEC-0019 §3）。单轮本身超出
    预算时才退到 `recent_output_chars` 兜底。
    """

    max_turns: int = Field(default=12, ge=1, le=200)
    tool_output_chars: int = Field(default=1600, ge=80, le=200_000)
    tool_output_head_ratio: float = Field(default=0.7, gt=0, lt=1)
    dedup_reads: bool = True
    # 尾部 N 个工具结果不参与内容截断：按「块」计数（并行 5 个读就是 5 块）
    recent_exempt_results: int = Field(default=4, ge=0, le=64)
    # 单轮本身超预算时的兜底上限，比旧轮次的 1600 宽，仍保留可复原的行号标记
    recent_output_chars: int = Field(default=6000, ge=200, le=200_000)
    # 关掉即回到「每轮无条件裁剪」，留给评测对照；生产保持开启
    trim_on_budget: bool = True


class EpisodicPolicy(BaseModel):
    """中期（会话情景）的卡片与版本策略。

    没有 `trigger_turns`：中期只由框架的压缩阈值触发（`trigger_ratio × window`），
    曾经存在的「每 N 轮压缩」从未有消费点，留着只会让调参的人以为改了有效果
    （SPEC-0013 §18.2 M2）。
    """

    enabled: bool = True
    # 卡片正文上限：既是写库前的截断，也是注入时的正文上限（与 note 预算取小）
    max_chars: int = Field(default=3000, ge=200, le=200_000)
    max_versions: int = Field(default=20, ge=1, le=500)
    ttl_days: int = Field(default=30, ge=1, le=3650)


class LongTermPolicy(BaseModel):
    """长期（语义 + 程序性）的后端与检索策略。

    `backend` 是**请求**而不是承诺：`mem0` / `agentic` 尚未接入，请求它们时
    会降级为 `local` 并在状态接口里写明 `requested` / `effective` / `reason`
    （SPEC-0013 §18.2 M1）。
    """

    enabled: bool = True
    backend: Literal["auto", "mem0", "agentic", "local", "off"] = "auto"
    scope: Literal["session", "user"] = "session"
    top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.35, ge=0, le=1)
    max_entries: int = Field(default=200, ge=1, le=10_000)
    ttl_days: int = Field(default=90, ge=1, le=3650)
    # 只有「回合结束写入」一种实现；`session_end` 曾在枚举里但从未被实现，
    # 收窄后写错值会在加载配置时就报错，而不是静默不写（SPEC-0013 §18.2 M4）。
    write_on: Literal["turn"] = "turn"


class MemoryBudgetPolicy(BaseModel):
    """三层在「历史面额度」里的分账比例；比例之和必须为 1。"""

    short_term_ratio: float = Field(default=0.50, ge=0, le=1)
    episodic_ratio: float = Field(default=0.30, ge=0, le=1)
    long_term_ratio: float = Field(default=0.10, ge=0, le=1)
    headroom_ratio: float = Field(default=0.10, ge=0, le=1)
    # 固定前缀的安全垫：估算器系统性低估时，先多留这么多比例，不要顶到窗口边界
    estimate_margin_ratio: float = Field(default=0.1, ge=0, le=1)

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
    # 每轮首次模型调用后对账「估算 vs 实测」；只发事件，不自动调参
    estimate_audit: bool = True


class MemoryTracePolicy(BaseModel):
    """轨迹预览长度：只放预览，不放全文。"""

    query_preview_chars: int = Field(default=120, ge=10, le=2000)
    hit_preview_chars: int = Field(default=80, ge=10, le=2000)
    summary_preview_chars: int = Field(default=160, ge=10, le=4000)


class MemoryPolicy(BaseModel):
    """记忆策略的完整类型化视图（三层 + 预算 + 上下文 + 轨迹）。

    没有 `compress` 段：曾经的 `fail_open` 没有任何消费点，删掉而不是留着当装饰
    （SPEC-0013 §18.8 M12）。压缩失败时的行为由 `CompressionMiddleware` 固定。
    """

    enabled: bool = True
    user_id: str = ""
    short_term: ShortTermPolicy = Field(default_factory=ShortTermPolicy)
    episodic: EpisodicPolicy = Field(default_factory=EpisodicPolicy)
    long_term: LongTermPolicy = Field(default_factory=LongTermPolicy)
    budget: MemoryBudgetPolicy = Field(default_factory=MemoryBudgetPolicy)
    context: MemoryContextPolicy = Field(default_factory=MemoryContextPolicy)
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
