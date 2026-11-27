"""输入解析编排：normalize → detect → expand → attach → redact → budget → compose。

任何阶段都不抛异常：解析失败只影响该阶段的状态与材料完整性，
对话本身照常进行。`on_secret` 是凭据落库钩子——管线不认存储，
由网关接上保险箱；不传就只脱敏、不保存（占位符仍会出现在正文里）。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Sequence
from pathlib import Path

from devops_agent.input.attachments import Attachment

from .budget import BudgetPolicy, _budget
from .detect import _detect
from .fetch import _attach, _expand, _remote_fetch_enabled
from .model import PreparedInput, Stage, _elapsed_ms, _stage
from .redact import SecretSink, _redact, _redact_text, _SecretNamer
from .render import _compose


def _normalize(text: str) -> tuple[str, Stage]:
    """统一换行、空白与空行：只做不改变语义的整理。"""
    start = time.perf_counter()
    out = text
    notes: list[str] = []
    if out.startswith("\ufeff"):
        out = out[1:]
        notes.append("去除 BOM")
    if "\r" in out:
        out = out.replace("\r\n", "\n").replace("\r", "\n")
        notes.append("CRLF → LF")
    if "\u3000" in out:
        out = out.replace("\u3000", " ")
        notes.append("全角空格归一")
    stripped = "\n".join(line.rstrip() for line in out.split("\n"))
    if stripped != out:
        notes.append("行尾空白")
        out = stripped
    squeezed = re.sub(r"\n{3,}", "\n\n", out)
    if squeezed != out:
        notes.append("连续空行压缩")
        out = squeezed
    out = out.strip()
    changed = out != text
    return out, _stage(
        "normalize",
        status="ok" if changed else "skip",
        summary=" · ".join(notes) if notes else "无需归一化",
        before=len(text),
        after=len(out),
        ms=_elapsed_ms(start),
        changed=changed,
    )


def _build_policy() -> BudgetPolicy:
    """调用时读包门面的预算常量，保证测试 monkeypatch 门面后仍生效。"""
    from devops_agent.input import pipeline as _pipeline

    return BudgetPolicy(total_chars=_pipeline.MAX_TOTAL_CHARS)


def default_workspace_root() -> Path | None:
    """本地引用解析的根目录：DEVOPS_AGENT_WORKSPACE 优先，否则当前工作目录。"""
    override = os.environ.get("DEVOPS_AGENT_WORKSPACE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.cwd()


def prepare(
    user_input: str,
    *,
    workspace_root: Path | None = None,
    remote_fetch: bool | None = None,
    attachments: Sequence[Attachment] = (),
    on_secret: SecretSink | None = None,
) -> PreparedInput:
    """按 normalize → detect → expand → attach → redact → budget → compose 解析输入。

    任何阶段都不抛异常：解析失败只影响该阶段的状态与材料完整性，
    对话本身照常进行。`on_secret` 是凭据落库钩子——管线不认存储，
    由网关接上保险箱；不传就只脱敏、不保存（占位符仍会出现在正文里）。
    """
    root = workspace_root if workspace_root is not None else default_workspace_root()
    namer = _SecretNamer()

    text, normalize_stage = _normalize(user_input)
    refs, detect_stage = _detect(text, root)
    blocks, ref_misses, thin_refs, expand_stage = _expand(
        refs, root, _remote_fetch_enabled(remote_fetch)
    )
    attached, attach_misses, attach_stage = _attach(attachments)
    text, all_blocks, redact_stage = _redact(text, blocks + attached, namer, on_secret)
    # 失败说明也要进模型上下文，因此同样过一遍脱敏（URL 里可能带 token）。
    # 这些内容来自用户原文或文件名，正文那一步已经走过 sink，这里不重复入库。
    misses = [_redact_text(miss, namer, None)[0] for miss in (*ref_misses, *attach_misses)]
    thin = tuple(_redact_text(value, namer, None)[0] for value in thin_refs)
    text, all_blocks, budget_stage = _budget(text, all_blocks, _build_policy())
    text, compose_stage = _compose(text, all_blocks, misses, thin)
    fallback = _fallback_summary(thin, ref_misses, attach_misses)

    return PreparedInput(
        original=user_input,
        text=text,
        stages=(
            normalize_stage,
            detect_stage,
            expand_stage,
            attach_stage,
            redact_stage,
            budget_stage,
            compose_stage,
        ),
        references=tuple(refs),
        redactions=redact_stage.items,
        fallback=fallback,
    )


def _fallback_summary(
    thin: Sequence[str], ref_misses: Sequence[str], attach_misses: Sequence[str]
) -> str:
    """缺口一句话（只描述事实）：怎么补由网关按 net 能力决定，管线不认识联网模式。"""
    parts: list[str] = []
    if thin:
        parts.append(f"{len(thin)} 条引用内容不足")
    # thin 已经在「内容不足」里算过，这里只报真正没取到的
    not_found = max(0, len(ref_misses) - len(thin))
    if not_found:
        parts.append(f"{not_found} 条引用未取到")
    if attach_misses:
        parts.append(f"{len(attach_misses)} 份附件未取到")
    return "，".join(parts)
