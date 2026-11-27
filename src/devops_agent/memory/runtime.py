"""记忆运行时 —— 把三层存储、预算与事件收口成一个会话级对象。

装配期每个会话一份；中间件与压缩钩子共用它，避免各自重复建仓储。
所有失败都只影响记忆功能本身：检索/写入异常会转成事件，不阻断对话。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentscope.agent import Agent

from devops_agent.config import (
    load_memory,
    load_model,
)
from devops_agent.input.pipeline.redact import redact_preview
from devops_agent.memory import events
from devops_agent.memory.budget import Budget, compute_budget
from devops_agent.memory.context import build_note, clip
from devops_agent.memory.episodic import EpisodicMemory
from devops_agent.memory.long_term import Hit, build_long_term
from devops_agent.memory.short_term import trim_context_for_snapshot
from devops_agent.memory.sink import MemorySink
from devops_agent.memory.tokens import estimate_tokens
from devops_agent.observability import repository as repo

# 长期写入只保留一段短文本：长期记忆是结论，不是原文仓库
_MAX_WRITE_CHARS = 1000


def _hit_hash(hits: list[Hit]) -> str:
    body = ",".join(str(hit.id) for hit in hits)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def _model_budget(agent: Agent, prefix_tokens: int, policy: Any) -> Budget:
    """从 agent 的真实窗口与 ContextConfig 推导预算；缺字段时回落 model.yaml。"""
    settings = load_model()
    config = getattr(agent, "context_config", None)
    trigger = float(getattr(config, "trigger_ratio", settings.trigger_ratio))
    reserve = float(getattr(config, "reserve_ratio", settings.reserve_ratio))
    model = getattr(agent, "model", None)
    size = int(getattr(model, "context_size", settings.context_size) or settings.context_size)
    return compute_budget(
        context_size=size,
        trigger_ratio=trigger,
        reserve_ratio=reserve,
        max_tokens=settings.max_tokens,
        prefix_tokens=prefix_tokens,
        short_term_ratio=policy.budget.short_term_ratio,
        episodic_ratio=policy.budget.episodic_ratio,
        long_term_ratio=policy.budget.long_term_ratio,
        headroom_ratio=policy.budget.headroom_ratio,
        episodic_enabled=policy.episodic.enabled,
        long_term_enabled=policy.long_term.enabled,
    )


@dataclass
class MemoryRuntime:
    """一个会话的记忆入口：三层存储 + 帧队列 + 预算。"""

    session_id: str
    policy: Any
    sink: MemorySink = field(default_factory=MemorySink)
    db_path: Path | None = None
    episodic: EpisodicMemory = field(init=False)
    long_term: Any = field(init=False)
    backend_name: str = field(init=False, default="off")
    backend_reason: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.episodic = EpisodicMemory(self.policy.episodic, self.db_path)
        scope_id = self.policy.user_id or f"session:{self.session_id}"
        self.long_term, self.backend_name = build_long_term(
            self.policy.long_term, scope_id, self.db_path
        )
        self.backend_reason = str(getattr(self.long_term, "reason", ""))

    # --- 预算 ---------------------------------------------------------
    def budget(self, agent: Agent, prefix_tokens: int) -> Budget:
        return _model_budget(agent, prefix_tokens, self.policy)

    # --- 召回 ---------------------------------------------------------
    async def recall(self, query: str) -> tuple[dict[str, Any] | None, list[Hit]]:
        """取 active 中期卡与长期命中；任一失败降级为空，不抛给主流程。"""
        card: dict[str, Any] | None = None
        hits: list[Hit] = []
        try:
            card = self.episodic.active(self.session_id)
        except Exception:  # noqa: BLE001 - 存储故障只影响记忆
            card = None
        try:
            hits = await self.long_term.search(
                query, self.policy.long_term.top_k, self.policy.long_term.min_score
            )
        except Exception:  # noqa: BLE001
            hits = []
        return card, hits

    def note(
        self,
        card: dict[str, Any] | None,
        hits: list[Hit],
    ) -> str:
        return build_note(
            card,
            [hit.as_dict() for hit in hits],
            note_max_chars=self.policy.context.note_max_chars,
            episodic_ratio=self.policy.context.episodic_note_ratio,
            summary_preview_chars=self.policy.trace.summary_preview_chars,
            top_k=self.policy.long_term.top_k,
        )

    @staticmethod
    def marker(card: dict[str, Any] | None, hits: list[Hit], reply_id: str) -> dict[str, Any]:
        return {
            "episodic_version": int(card.get("version", 0)) if card else 0,
            "long_term_hash": _hit_hash(hits),
            "reply_id": reply_id,
        }

    # --- 压缩落库 -----------------------------------------------------
    def record_compression(
        self,
        summary: str,
        *,
        before: int,
        after: int,
        threshold: int,
        trigger: str = "context_threshold",
        artifacts: list[str] | None = None,
        pending: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """压缩成功后写中期新版本，并把结果补成轨迹事件。

        `artifacts/pending/errors` 由评测 harness 传入：压缩保真赛道要核对
        产物与约束是否随摘要一起保留，生产调用方不传则保持原行为。
        """
        start = time.perf_counter()
        card: dict[str, Any] | None = None
        try:
            if self.policy.episodic.enabled and summary.strip():
                card = self.episodic.save(
                    self.session_id,
                    text=summary,
                    trigger=trigger,
                    tokens_before=before,
                    tokens_after=after,
                    budget=threshold,
                    artifacts=artifacts,
                    pending=pending,
                    errors=errors,
                )
        except Exception as exc:  # noqa: BLE001 - 压缩失败不阻断对话
            self.sink.emit(
                events.frame(
                    events.MEMORY_COMPRESS_END,
                    layer="episodic",
                    state="error",
                    reason=f"{type(exc).__name__}: {exc}",
                    tokens_before=before,
                    tokens_after=after,
                    budget=threshold,
                    trigger=trigger,
                    latency_ms=int((time.perf_counter() - start) * 1000),
                )
            )
            return None
        latency = int((time.perf_counter() - start) * 1000)
        self.sink.emit(
            events.frame(
                events.MEMORY_COMPRESS_END,
                layer="episodic",
                tokens_before=before,
                tokens_after=after,
                budget=threshold,
                trigger=trigger,
                version=int(card["version"]) if card else 0,
                latency_ms=latency,
            )
        )
        if card:
            self.sink.emit(
                events.frame(
                    events.MEMORY_WRITE_END,
                    layer="episodic",
                    stored=True,
                    deduped=False,
                    version=int(card["version"]),
                    summary_chars=len(summary),
                )
            )
        return card

    # --- 写入长期 -----------------------------------------------------
    async def write_turn(self, user_text: str, assistant_text: str) -> dict[str, Any]:
        """回合结束写长期候选：只写脱敏后的短结论，去重交给后端。"""
        if not self.policy.long_term.enabled or self.policy.long_term.write_on != "turn":
            return {"stored": False, "deduped": False, "skipped": True}
        raw = f"{user_text.strip()}\n{assistant_text.strip()}".strip()
        text = clip(redact_preview(raw), _MAX_WRITE_CHARS)
        if len(text) < 8:
            return {"stored": False, "deduped": False, "skipped": True}
        start = time.perf_counter()
        self.sink.emit(events.frame(events.MEMORY_WRITE_START, layer="long"))
        try:
            result = await self.long_term.write(text)
        except Exception as exc:  # noqa: BLE001
            self.sink.emit(
                events.frame(
                    events.MEMORY_WRITE_END,
                    layer="long",
                    state="error",
                    reason=f"{type(exc).__name__}: {exc}",
                    latency_ms=int((time.perf_counter() - start) * 1000),
                )
            )
            return {"stored": False, "deduped": False, "error": str(exc)}
        self.sink.emit(
            events.frame(
                events.MEMORY_WRITE_END,
                layer="long",
                stored=bool(result.get("stored")),
                deduped=bool(result.get("deduped")),
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
        )
        return dict(result)

    # --- 快照 ---------------------------------------------------------
    def cap_snapshot(self, agent: Agent) -> None:
        """序列化前收缩历史，避免 state_json 无上限增长。"""
        context = list(getattr(agent.state, "context", []) or [])
        trimmed, report = trim_context_for_snapshot(
            context, self.policy.short_term, self.policy.context.snapshot_max_chars
        )
        if report.changed:
            agent.state.context = trimmed

    def status(self) -> dict[str, Any]:
        settings = load_model()
        return {
            "enabled": bool(self.policy.enabled),
            "short_term": bool(self.policy.short_term.max_turns > 0),
            "episodic": bool(self.policy.episodic.enabled),
            "long_term": bool(self.policy.long_term.enabled),
            "backend": self.backend_name,
            "reason": self.backend_reason,
            "context": {
                "window": settings.context_size,
                "threshold": int(settings.trigger_ratio * settings.context_size),
                "snapshot_version": self.policy.context.snapshot_version,
            },
        }


def memory_status() -> dict[str, Any]:
    """进程级记忆状态（/health 与 /api/memory/status 共用）。"""
    policy = load_memory()
    runtime = MemoryRuntime("", policy, MemorySink(), None)
    state = runtime.status()
    state["policy"] = {
        "short_term_ratio": policy.budget.short_term_ratio,
        "episodic_ratio": policy.budget.episodic_ratio,
        "long_term_ratio": policy.budget.long_term_ratio,
        "headroom_ratio": policy.budget.headroom_ratio,
    }
    return state


def session_counts(session_id: str) -> dict[str, int]:
    """某个会话的两层条数（管理接口用）；读不到时返回 0。"""
    policy = load_memory()
    scope_id = policy.user_id or f"session:{session_id}"
    try:
        with repo.connect() as conn:
            repo.init_db(conn)
            return {
                "versions": len(repo.list_memory_versions(conn, session_id, 100)),
                "long_term": repo.count_long_term(conn, policy.long_term.scope, scope_id),
            }
    except Exception:  # noqa: BLE001
        return {"versions": 0, "long_term": 0}


def estimate_prefix_tokens(system_text: str, tools: list[dict[str, Any]]) -> int:
    """固定前缀 token 估算：系统提示 + 工具 schema。"""
    import json

    return estimate_tokens(system_text) + estimate_tokens(json.dumps(tools, ensure_ascii=False))
