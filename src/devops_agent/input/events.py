"""输入解析事件契约 —— 网关发、前端画、recorder 落库共用同一套 key。

三个事件构成一段可独立识别的前置过程（不经过 AgentScope，也不进模型上下文）：
- INPUT_PARSE_START：建组，前端据此在轨迹里开一个「输入解析」节点；
- INPUT_PARSE_STAGE：每个阶段一条，带 before/after/items/ms/changed；
- INPUT_PARSE_END：收组，带一句总账，供节点行直接显示。

key 一经确定不再随实现改动：前端折叠逻辑与数据库列都按它对齐。
"""

from __future__ import annotations

from datetime import UTC, datetime

from devops_agent.input.pipeline import PreparedInput, Stage, estimate_tokens

BLOCK_ID = "input-parse"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def start_event(created_at: str | None = None) -> dict[str, object]:
    return {"type": "INPUT_PARSE_START", "block_id": BLOCK_ID, "created_at": created_at or _now()}


def stage_event(stage: Stage, created_at: str | None = None) -> dict[str, object]:
    return {
        "type": "INPUT_PARSE_STAGE",
        "block_id": BLOCK_ID,
        "created_at": created_at or _now(),
        "stage": stage.stage,
        "stage_label": stage.label,
        "state": stage.status,
        "summary": stage.summary,
        "before": stage.before,
        "after": stage.after,
        "items": stage.items,
        "ms": stage.ms,
        "changed": stage.changed,
        "failed": stage.failed,
        "detail": stage.detail,
    }


def end_event(prepared: PreparedInput, created_at: str | None = None) -> dict[str, object]:
    effective = prepared.effective
    attachments = next((stage.items for stage in prepared.stages if stage.stage == "attach"), 0)
    counts: list[str] = []
    if attachments:
        counts.append(f"{attachments} 附件")
    if prepared.references:
        counts.append(f"{len(prepared.references)} 引用")
    if prepared.redactions:
        counts.append(f"{prepared.redactions} 脱敏")
    if prepared.degraded:
        counts.append(f"{prepared.degraded} 降级")
    summary = " · ".join(
        [
            f"{len(prepared.stages)} 阶段",
            f"{len(prepared.original)} → {len(prepared.text)} 字",
            f"≈ {estimate_tokens(prepared.text)} token",
            *counts,
        ]
    )
    return {
        "type": "INPUT_PARSE_END",
        "block_id": BLOCK_ID,
        "created_at": created_at or _now(),
        "summary": summary,
        "changed_stages": len(effective),
        "degraded": prepared.degraded,
    }


def events(prepared: PreparedInput, created_at: str | None = None) -> list[dict[str, object]]:
    """一次解析的完整事件序列：START + 每阶段一条 + END。"""
    stamp = created_at or _now()
    frames = [start_event(stamp)]
    frames.extend(stage_event(stage, stamp) for stage in prepared.stages)
    frames.append(end_event(prepared, stamp))
    return frames
