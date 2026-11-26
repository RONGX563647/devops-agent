"""拼装最终交给模型的文本：用户原话 + 材料附录 + 未取到的材料。

网络材料一律包进 `<<untrusted-web …>>` 边界（SPEC-0012 §5）：这是给模型的**结构信号**，
不是给渲染用的样式。正文里的「忽略以上指令」由此有明确归属——它是资料，不是命令。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from devops_agent.config import load_input

from .model import Reference, Stage, _elapsed_ms, _stage, estimate_tokens

UNTRUSTED_OPEN = '<<untrusted-web source="{source}" bytes="{size}">>'
UNTRUSTED_CLOSE = "<<untrusted-web-end>>"


def _wrap_untrusted(source: str, content: str) -> str:
    """给外部内容加不可信边界；开关关闭时原样返回（排障用，不默认）。"""
    if not load_input().wrap_untrusted:
        return content
    return (
        UNTRUSTED_OPEN.format(source=source, size=len(content))
        + "\n"
        + content
        + "\n"
        + UNTRUSTED_CLOSE
    )


def _render_materials(blocks: Sequence[tuple[Reference, str]], thin: Sequence[str] = ()) -> str:
    """取到的材料：一份一个小节，正文包进代码块 + 不可信边界。

    `thin` 里的引用仍保留内容（几十个字也可能有用），但标题要标「内容不足」——
    否则模型会把它当完整资料，答完才发现缺页。
    """
    thin_set = set(thin)
    return "\n\n".join(
        f"### 引用材料 {index} · {ref.value}"
        + ("（内容不足）" if ref.value in thin_set else "")
        + "\n\n```\n"
        + _wrap_untrusted(ref.value, content)
        + "\n```"
        for index, (ref, content) in enumerate(blocks, start=1)
    )


def _render_misses(misses: Sequence[str]) -> str:
    """取不到 / 内容不足的材料：清单 + 停止规则。

    只列清单不够。实测一次会话里模型不知道引用抓取失败（失败原因只在阶段详情里），
    自己去 curl 同一个地址，被 shell 拆参数、被网络重置，换 PowerShell、换 python
    脚本、换代理设置，烧掉 21 步工具调用才承认拿不到。所以这里必须同时说清
    「别再抓了」和「拿已有的回答」。
    """
    body = "\n".join(f"- {miss}" for miss in misses)
    lines = [
        "### 本轮未能取到的材料",
        "",
        _wrap_untrusted("misses", body),
        "",
        "以上内容本轮确实拿不到，不要用工具重复抓取同一地址或重读同一文件："
        "同一目标失败两次就停止尝试。",
        "标为「内容不足」的引用可能有别的页面包含同样信息，可以换来源，但不要重复抓同一地址。",
        "请基于已拿到的材料与已知信息回答，并明确说明还缺哪些内容。",
    ]
    return "\n".join(lines)


def _compose(
    text: str,
    blocks: list[tuple[Reference, str]],
    misses: Sequence[str] = (),
    thin: Sequence[str] = (),
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
        sections.append(_render_materials(blocks, thin))
    if misses:
        sections.append(_render_misses(misses))
    merged = "\n\n---\n\n".join(sections)
    tokens = estimate_tokens(merged)
    counts = [f"{len(blocks)} 份材料"] if blocks else []
    if thin:
        counts.append(f"{len(thin)} 份内容不足")
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
