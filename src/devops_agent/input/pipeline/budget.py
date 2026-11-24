"""预算裁剪：先去噪，再按份额裁材料，最后才截正文。"""

from __future__ import annotations

import time
from typing import Final

from .denoise import _denoise, _truncate
from .model import Reference, Stage, _elapsed_ms, _stage

# 单次输入的总字符预算（正文 + 全部材料）：超出后按「先去噪、再取舍」处理
MAX_TOTAL_CHARS: Final = 24_000
# 预算里留给材料的份额：材料再多也不能把用户原话挤没
MATERIAL_SHARE: Final = 0.65
# 单份材料裁剪后的下限：留 200 字通常还能看出「这是什么文件」
MIN_MATERIAL_CHARS: Final = 200


def _trim_blocks(
    blocks: list[tuple[Reference, str]], budget: int
) -> tuple[list[tuple[Reference, str]], int]:
    """按块均分材料预算；返回 (新块列表, 被裁剪的块数)。"""
    if not blocks:
        return [], 0
    per = max(MIN_MATERIAL_CHARS, budget // len(blocks))
    trimmed = 0
    out: list[tuple[Reference, str]] = []
    for ref, content in blocks:
        if len(content) > per:
            trimmed += 1
            content = _truncate(content, per)
        out.append((ref, content))
    return out, trimmed


def _denoise_blocks(
    blocks: list[tuple[Reference, str]],
) -> tuple[list[tuple[Reference, str]], list[str]]:
    """对每份材料去噪，返回 (新块, 改动说明)；说明带来源名，便于在轨迹里定位。"""
    notes: list[str] = []
    out: list[tuple[Reference, str]] = []
    for ref, content in blocks:
        cleaned, block_notes = _denoise(content)
        notes.extend(f"{ref.value} {note}" for note in block_notes)
        out.append((ref, cleaned))
    return out, notes


def _shrink_materials(
    blocks: list[tuple[Reference, str]], limit: int
) -> tuple[list[tuple[Reference, str]], list[str]]:
    """材料按预算份额裁剪：最多占 limit 的 MATERIAL_SHARE，剩下的留给正文。"""
    available = sum(len(content) for _, content in blocks)
    shrunk, trimmed = _trim_blocks(blocks, min(available, int(limit * MATERIAL_SHARE)))
    return shrunk, ([f"材料裁剪 ×{trimmed}"] if trimmed else [])


def _budget(
    text: str, blocks: list[tuple[Reference, str]], limit: int | None = None
) -> tuple[str, list[tuple[Reference, str]], Stage]:
    """对「正文 + 材料」整体做预算：先去噪，再按份额裁剪，最后才截正文。

    limit 缺省读模块常量（而不是写进默认参数）：测试要能改小预算验证裁剪。
    """
    # 运行期从包门面取 MAX_TOTAL_CHARS：测试会 monkeypatch pipeline.MAX_TOTAL_CHARS，
    # 这里必须读到被替换后的值，不能绑死在本模块的全局名上。
    from devops_agent.input import pipeline as _pipeline

    limit = _pipeline.MAX_TOTAL_CHARS if limit is None else limit
    start = time.perf_counter()
    before = len(text) + sum(len(content) for _, content in blocks)
    if before <= limit:
        return (
            text,
            blocks,
            _stage(
                "budget",
                status="skip",
                summary=f"{before} 字 · 未超预算（{limit}）",
                before=before,
                after=before,
                ms=_elapsed_ms(start),
                changed=False,
            ),
        )

    work, text_notes = _denoise(text)
    work_blocks, block_notes = _denoise_blocks(blocks)
    work_blocks, trim_notes = _shrink_materials(work_blocks, limit)
    notes = [f"正文{note}" for note in text_notes] + block_notes + trim_notes
    reserved = sum(len(content) for _, content in work_blocks)
    work = _truncate(work, max(MIN_MATERIAL_CHARS, limit - reserved))
    after = len(work) + reserved
    if after < before:
        notes.append(f"{before} → {after} 字")

    return (
        work,
        work_blocks,
        _stage(
            "budget",
            status="ok",
            summary=" · ".join(notes),
            before=before,
            after=after,
            ms=_elapsed_ms(start),
            detail="\n".join(notes),
            changed=True,
        ),
    )
