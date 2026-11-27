"""记忆运行时 —— 把三层存储、预算与事件收口成一个会话级对象。

装配期每个会话一份；中间件与压缩钩子共用它，避免各自重复建仓储。
所有失败都只影响记忆功能本身：检索/写入异常会转成事件，不阻断对话。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from agentscope.agent import Agent

from devops_agent.config import (
    load_memory,
    load_model,
)
from devops_agent.input.pipeline.redact import redact_preview
from devops_agent.memory import events, health
from devops_agent.memory.budget import Budget, compute_budget
from devops_agent.memory.candidate import is_candidate
from devops_agent.memory.context import build_note, clip
from devops_agent.memory.episodic import EpisodicMemory
from devops_agent.memory.long_term import Hit, build_long_term
from devops_agent.memory.short_term import TrimReport, trim_context_for_snapshot
from devops_agent.memory.sink import MemorySink
from devops_agent.memory.tokens import estimate_tokens
from devops_agent.observability import repository as repo

# 长期写入只保留一段短文本：长期记忆是结论，不是原文仓库
_MAX_WRITE_CHARS = 1000


def _hit_hash(hits: list[Hit]) -> str:
    body = ",".join(str(hit.id) for hit in hits)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class RecallResult:
    """一轮召回的完整结果，含降级信息。

    为什么不是一个 `(card, hits)` 元组：空元组既可能是「确实没有」，也可能是
    「后端挂了被吞掉」。调用方要能区分，否则故障永远显示成正常的 miss。
    """

    card: dict[str, Any] | None
    hits: list[Hit]
    degraded: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)
    # 层的异常明细（`Type: msg`）：reason 给稳定枚举，errors 留给排障
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        """降级原因（多层级用逗号连接）；无降级时为空串。"""
        return ",".join(self.reasons[layer] for layer in self.degraded if layer in self.reasons)


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
        estimate_margin_ratio=policy.budget.estimate_margin_ratio,
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
    backend_requested: str = field(init=False, default="")
    backend_reason: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.episodic = EpisodicMemory(self.policy.episodic, self.db_path)
        scope_id = self.policy.user_id or f"session:{self.session_id}"
        choice = build_long_term(self.policy.long_term, scope_id, self.db_path)
        self.long_term = choice.backend
        # backend_name 是「实际生效」的那个：事件与 /health 都拿它，避免把请求值当结果
        self.backend_name = choice.effective
        self.backend_requested = choice.requested
        self.backend_reason = choice.reason or str(getattr(choice.backend, "reason", ""))

    # --- 预算 ---------------------------------------------------------
    def budget(self, agent: Agent, prefix_tokens: int) -> Budget:
        return _model_budget(agent, prefix_tokens, self.policy)

    # --- 召回 ---------------------------------------------------------
    async def recall(self, query: str) -> RecallResult:
        """取 active 中期卡与长期命中；失败降级为空并记健康，不抛给主流程。

        失败必须能被调用方看见：`degraded` 记哪几层坏了（事件据此从 `miss`
        改判 `error`），`reasons` 给稳定枚举，`errors` 留 `Type: msg` 供排障
        （SPEC-0013 §18.2 M8）。
        """
        degraded: list[str] = []
        reasons: dict[str, str] = {}
        errors: dict[str, str] = {}
        card: dict[str, Any] | None = None
        try:
            card = self.episodic.active(self.session_id)
        except Exception as exc:  # noqa: BLE001 - 存储故障只影响记忆
            degraded.append("episodic")
            reasons["episodic"] = events.REASON_EPISODIC_UNAVAILABLE
            errors["episodic"] = f"{type(exc).__name__}: {exc}"
            health.note_failure("episodic", reasons["episodic"])
        else:
            health.note_success("episodic")
        hits: list[Hit] = []
        try:
            hits = await self.long_term.search(
                query, self.policy.long_term.top_k, self.policy.long_term.min_score
            )
        except Exception as exc:  # noqa: BLE001
            degraded.append("long_term")
            reasons["long_term"] = events.REASON_LONG_TERM_UNAVAILABLE
            errors["long_term"] = f"{type(exc).__name__}: {exc}"
            health.note_failure("long_term", reasons["long_term"])
        else:
            health.note_success("long_term")
        return RecallResult(card, hits, tuple(degraded), reasons, errors)

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
            card_max_chars=self.policy.episodic.max_chars,
            top_k=self.policy.long_term.top_k,
        )

    @staticmethod
    def marker(card: dict[str, Any] | None, hits: list[Hit], turn_key: str) -> dict[str, Any]:
        """本轮的注入标记：只在「同一个 turn」内复用，跨 turn 用来判断要不要重注入。"""
        return {
            "episodic_version": int(card.get("version", 0)) if card else 0,
            "long_term_hash": _hit_hash(hits),
            "turn_key": turn_key,
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
        """回合结束写长期候选：先过滤，再脱敏截断，去重交给后端。

        过滤放在写入前而不是写入后：一条寒暄一旦进了长期层，就会在后面的
        每一轮里被召回，删起来还得人工找（SPEC-0013 §18.2 M9）。
        """
        if not self.policy.long_term.enabled or self.policy.long_term.write_on != "turn":
            return {"stored": False, "deduped": False, "skipped": True, "reason": "disabled"}
        ok, reason = is_candidate(user_text, assistant_text)
        if not ok:
            # 发事件而不是静默跳过：否则「为什么这句没记住」只能靠猜
            self.sink.emit(
                events.frame(events.MEMORY_WRITE_END, layer="long", state="skipped", reason=reason)
            )
            return {"stored": False, "deduped": False, "skipped": True, "reason": reason}
        raw = f"{user_text.strip()}\n{assistant_text.strip()}".strip()
        text = clip(redact_preview(raw), _MAX_WRITE_CHARS)
        start = time.perf_counter()
        self.sink.emit(events.frame(events.MEMORY_WRITE_START, layer="long"))
        try:
            result = await self.long_term.write(text)
        except Exception as exc:  # noqa: BLE001
            health.note_failure("long_term", events.REASON_WRITE_FAILED)
            self.sink.emit(
                events.frame(
                    events.MEMORY_WRITE_END,
                    layer="long",
                    state="error",
                    reason=events.REASON_WRITE_FAILED,
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc)[:200],
                    latency_ms=int((time.perf_counter() - start) * 1000),
                )
            )
            return {"stored": False, "deduped": False, "error": str(exc)}
        health.note_success("long_term")
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
    def cap_snapshot(self, agent: Agent) -> TrimReport:
        """序列化前收缩历史，避免 state_json 无上限增长。

        只写回「因体积丢掉的整轮」。内容截断是请求期的临时行为，写回
        `state.context` 会让它变成永久丢失——切档与重建会话都会触发快照，
        而报告里只有 `changed` 一个信号时，分不清这两种丢失（SPEC-0019 §5.2）。

        裁剪必须发事件：`MEMORY_TRIM(reason=snapshot_cap)` 是「这一轮不是请求裁剪
        丢的历史，而是快照时丢的」的唯一证据，丢了它就只能看到上下文莫名变短
        （SPEC-0013 §18.2 M6）。
        """
        context = list(getattr(agent.state, "context", []) or [])
        trimmed, report = trim_context_for_snapshot(
            context, self.policy.short_term, self.policy.context.snapshot_max_chars
        )
        if report.dropped_turns <= 0:
            return report
        agent.state.context = trimmed
        self.sink.emit(
            events.frame(
                events.MEMORY_TRIM,
                layer="short",
                reason="snapshot_cap",
                **cast(dict[str, Any], report.as_dict()),
            )
        )
        return report

    def status(self) -> dict[str, Any]:
        settings = load_model()
        return {
            "enabled": bool(self.policy.enabled),
            "short_term": bool(self.policy.short_term.max_turns > 0),
            "episodic": bool(self.policy.episodic.enabled),
            "long_term": bool(self.policy.long_term.enabled),
            # backend 保留为 effective 的别名（/health 与前端都在用它）；
            # requested/effective 分开报，才看得出「配了 mem0、跑的是 local」。
            "backend": self.backend_name,
            "requested": self.backend_requested,
            "effective": self.backend_name,
            "reason": self.backend_reason,
            "context": {
                "window": settings.context_size,
                "threshold": int(settings.trigger_ratio * settings.context_size),
                "snapshot_version": self.policy.context.snapshot_version,
            },
            # 开关说明「打算做什么」，health 说明「当下是否在降级」——两者缺一不可
            "health": health.snapshot(),
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
