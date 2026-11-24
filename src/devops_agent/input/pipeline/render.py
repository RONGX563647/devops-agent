"""拼装最终交给模型的文本：用户原话 + 材料附录 + 未取到的材料。"""

from __future__ import annotations

import time
from collections.abc import Sequence

from .model import Reference, Stage, _elapsed_ms, _stage, estimate_tokens


def _render_materials(blocks: Sequence[tuple[Reference, str]]) -> str:
    """取到的材料：一份一个小节，正文原样包进代码块，避免被当成指令读。"""
    return "\n\n".join(
        f"### 引用材料 {index} · {ref.value}\n\n```\n{content}\n```"
        for index, (ref, content) in enumerate(blocks, start=1)
    )


def _render_misses(misses: Sequence[str]) -> str:
    """取不到的材料：清单 + 停止规则。

    只列清单不够。实测一次会话里模型不知道引用抓取失败（失败原因只在阶段详情里），
    自己去 curl 同一个地址，被 shell 拆参数、被网络重置，换 PowerShell、换 python
    脚本、换代理设置，烧掉 21 步工具调用才承认拿不到。所以这里必须同时说清
    「别再抓了」和「拿已有的回答」。
    """
    lines = ["### 本轮未能取到的材料", ""]
    lines.extend(f"- {miss}" for miss in misses)
    lines.extend(
        [
            "",
            "以上内容本轮确实拿不到，不要用工具重复抓取同一地址或重读同一文件："
            "同一目标失败两次就停止尝试。",
            "请基于已拿到的材料与已知信息回答，并明确说明还缺哪些内容。",
        ]
    )
    return "\n".join(lines)


def _compose(
    text: str,
    blocks: list[tuple[Reference, str]],
    misses: Sequence[str] = (),
) -> tuple[str, Stage]:
    """拼出最终交给模型的文本：用户原话 + 材料附录 + 未取到的材料。"""
    start = time.perf_counter()
    before = len(text)
    if not blocks and not misses:
        return text, _stage(
            "compose",
            status="skip",
            summary="原样透传",
            before=before,
            after=before,
            ms=_elapsed_ms(start),
            changed=False,
        )
    sections = [text]
    if blocks:
        sections.append(_render_materials(blocks))
    if misses:
        sections.append(_render_misses(misses))
    merged = "\n\n---\n\n".join(sections)
    tokens = estimate_tokens(merged)
    counts = [f"{len(blocks)} 份材料"] if blocks else []
    if misses:
        counts.append(f"未取到 {len(misses)} 份")
    return merged, _stage(
        "compose",
        status="ok",
        summary=f"{' · '.join(counts)} · 最终 {len(merged)} 字 ≈ {tokens} token",
        before=before,
        after=len(merged),
        items=len(blocks) + len(misses),
        ms=_elapsed_ms(start),
        changed=True,
    )
