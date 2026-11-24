"""预算裁剪：先去噪，再按份额裁材料，最后才截正文。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

from .denoise import _denoise, _truncate
from .model import Reference, Stage, _elapsed_ms, _stage

# 单次输入的总字符预算（正文 + 全部材料）：超出后按「先去噪、再取舍」处理
MAX_TOTAL_CHARS: Final = 24_000
# 预算里留给材料的份额：材料再多也不能把用户原话挤没
MATERIAL_SHARE: Final = 0.65
# 单份材料裁剪后的下限：留 200 字通常还能看出「这是什么文件」
MIN_MATERIAL_CHARS: Final = 200

Block = tuple[Reference, str]


@dataclass(frozen=True)
class BudgetPolicy:
    """一次预算裁剪的阈值快照。

    由编排层在调用时构造并传入，函数内不再回查包门面，
    `monkeypatch.setattr(pipeline, "MAX_TOTAL_CHARS", ...)` 依旧生效。
    """

    total_chars: int
    min_material_chars: int = MIN_MATERIAL_CHARS


def _trim_blocks(blocks: list[Block], budget: int) -> tuple[list[Block], int]:
    """按块均分材料预算；返回 (新块列表, 被裁剪的块数)。"""
    if not blocks:
        return [], 0
    per = max(MIN_MATERIAL_CHARS, budget // len(blocks))
    trimmed = 0
    out: list[Block] = []
    for ref, content in blocks:
        if len(content) > per:
            trimmed += 1
            content = _truncate(content, per)
        out.append((ref, content))
    return out, trimmed


def _denoise_blocks(blocks: list[Block]) -> tuple[list[Block], list[str]]:
    """对每份材料去噪，返回 (新块, 改动说明)；说明带来源名，便于在轨迹里定位。"""
    notes: list[str] = []
    out: list[Block] = []
    for ref, content in blocks:
        cleaned, block_notes = _denoise(content)
        notes.extend(f"{ref.value} {note}" for note in block_notes)
        out.append((ref, cleaned))
    return out, notes


def _shrink_materials(blocks: list[Block], limit: int) -> tuple[list[Block], list[str]]:
    """材料按预算份额裁剪：最多占 limit 的 MATERIAL_SHARE，剩下的留给正文。"""
    available = sum(len(content) for _, content in blocks)
    shrunk, trimmed = _trim_blocks(blocks, min(available, int(limit * MATERIAL_SHARE)))
    return shrunk, ([f"材料裁剪 ×{trimmed}"] if trimmed else [])


def _measure(text: str, blocks: list[Block]) -> int:
    """量体积：正文加全部材料的字符数。"""
    return len(text) + sum(len(content) for _, content in blocks)


def _shrink(
    text: str, blocks: list[Block], policy: BudgetPolicy
) -> tuple[str, list[Block], list[str]]:
    """去噪 → 按份额裁材料 → 截正文；返回 (正文, 材料, 改动说明)。"""
    work, text_notes = _denoise(text)
    work_blocks, block_notes = _denoise_blocks(blocks)
    work_blocks, trim_notes = _shrink_materials(work_blocks, policy.total_chars)
    notes = [f"正文{note}" for note in text_notes] + block_notes + trim_notes
    reserved = _measure("", work_blocks)
    work = _truncate(work, max(policy.min_material_chars, policy.total_chars - reserved))
    return work, work_blocks, notes


def _budget_stage(
    *,
    start: float,
    status: str,
    summary: str,
    before: int,
    after: int,
    changed: bool,
    detail: str = "",
) -> Stage:
    """构造 budget 阶段的 Stage 记录。"""
    return _stage(
        "budget",
        status=status,
        summary=summary,
        before=before,
        after=after,
        ms=_elapsed_ms(start),
        detail=detail,
        changed=changed,
    )


def _budget(text: str, blocks: list[Block], policy: BudgetPolicy) -> tuple[str, list[Block], Stage]:
    """对「正文 + 材料」整体做预算：先去噪，再按份额裁剪，最后才截正文。

    阈值由调用方以 BudgetPolicy 传入（prepare 每次调用时读取包门面的
    MAX_TOTAL_CHARS 再构造），函数内不再做运行期门面查找。
    """
    start = time.perf_counter()
    # 1. 量出当前体积：正文 + 全部材料。
    before = _measure(text, blocks)
    # 2. 未超预算直接早退，保持「无需处理」不被记成一次改动。
    if before <= policy.total_chars:
        return (
            text,
            blocks,
            _budget_stage(
                start=start,
                status="skip",
                summary=f"{before} 字 · 未超预算（{policy.total_chars}）",
                before=before,
                after=before,
                changed=False,
            ),
        )
    # 3. 超预算才动手：去噪 → 按份额裁材料 → 截正文。
    work, work_blocks, notes = _shrink(text, blocks, policy)
    # 4. 复测体积，有下降就补一句总量变化，便于在轨迹里核对。
    after = _measure(work, work_blocks)
    if after < before:
        notes.append(f"{before} → {after} 字")
    # 5. 组装阶段记录（文案与字段保持原样）。
    return (
        work,
        work_blocks,
        _budget_stage(
            start=start,
            status="ok",
            summary=" · ".join(notes),
            before=before,
            after=after,
            detail="\n".join(notes),
            changed=True,
        ),
    )
