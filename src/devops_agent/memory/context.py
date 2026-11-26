"""记忆注入 —— 把中期卡与长期命中装配成请求尾部的 system note。

不变量：
- 只注入「有内容」的段；空段不产生标题，避免每轮多一段无效文本。
- 中期卡先于长期命中；调用方保证档位说明在两者之前。
- 只放预览与结论，不放原文；长度按字符预算硬截断。
"""

from __future__ import annotations

from typing import Any

EPISODIC_HEADER = "## 会话记忆（第 {version} 版）"
LONG_TERM_HEADER = "## 长期记忆（检索 top_k={top_k}，命中 {hits} 条）"


def clip(text: str, limit: int) -> str:
    """按字符截断，尾部留一个省略标记；limit<=0 时返回空串。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _bullets(items: list[str], limit: int) -> str:
    if not items:
        return ""
    return "\n".join(f"- {clip(item, limit)}" for item in items)


def format_episodic(card: dict[str, Any], *, max_chars: int, summary_preview_chars: int) -> str:
    """把中期卡渲染成一段；text 是权威摘要，其余字段只做补充。"""
    version = int(card.get("version", 0) or 0)
    parts = [EPISODIC_HEADER.format(version=version)]
    text = str(card.get("text") or "").strip()
    if text:
        parts.append(clip(text, summary_preview_chars))
    artifacts = [str(item) for item in card.get("artifacts") or []]
    pending = [str(item) for item in card.get("pending") or []]
    errors = [str(item) for item in card.get("errors") or []]
    if artifacts:
        parts.append("产物：\n" + _bullets(artifacts, max_chars))
    if pending:
        parts.append("未决：\n" + _bullets(pending, max_chars))
    if errors:
        parts.append("错误：\n" + _bullets(errors, max_chars))
    return clip("\n".join(parts), max_chars)


def format_long_term(hits: list[dict[str, Any]], *, top_k: int, max_chars: int) -> str:
    """把检索命中渲染成编号列表；不命中返回空串。"""
    if not hits:
        return ""
    header = LONG_TERM_HEADER.format(top_k=top_k, hits=len(hits))
    lines = [
        f"[{index}] (score={float(hit.get('score', 0.0)):.2f}) {str(hit.get('text', ''))}"
        for index, hit in enumerate(hits, start=1)
    ]
    return clip(header + "\n" + "\n".join(lines), max_chars)


def build_note(
    card: dict[str, Any] | None,
    hits: list[dict[str, Any]],
    *,
    note_max_chars: int,
    episodic_ratio: float,
    summary_preview_chars: int,
    top_k: int,
) -> str:
    """中期 + 长期合成为一条 note；两段都空时返回空串。"""
    episodic_budget = int(note_max_chars * episodic_ratio)
    long_budget = max(0, note_max_chars - episodic_budget)
    sections: list[str] = []
    if card:
        episodic = format_episodic(
            card, max_chars=episodic_budget, summary_preview_chars=summary_preview_chars
        )
        if episodic:
            sections.append(episodic)
    long_term = format_long_term(hits, top_k=top_k, max_chars=long_budget)
    if long_term:
        sections.append(long_term)
    return "\n\n".join(sections)
