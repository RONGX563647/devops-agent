"""外部内容的安全裁剪：净化 → 抽正文 → 归一 → 注入标记 → 裁剪。

为什么单独成模块：L0（服务端预抓取）与 L2（`web_search` 结果）必须用同一套规则，
两处各写一遍必然漂移 —— 一处记得去掉 `<script>`、另一处忘了，就等于没做。

依赖策略（可选 extra `[sanitize]`）：

| 依赖 | 用途 | 没装时 |
|---|---|---|
| `nh3` | HTML 净化（Rust ammonia 绑定） | 退化为标签/危险块剥离 |
| `trafilatura` | 正文抽取 | 退化为标签剥离 + 归一 |

两样都没有也能跑，但 `capabilities()` 会如实上报降级，由调用方写进 `/health` 与阶段详情 ——
不静默降级是本模块的硬约束。

所有函数都不抛异常：这是「让对话继续」的一层，不是校验层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

# 允许保留的标签与属性：只留结构与文本，脚本、样式、表单、iframe 一律清掉
_ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "a", "b", "blockquote", "br", "code", "dd", "div", "dl", "dt", "em",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre",
        "s", "span", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead",
        "tr", "u", "ul",
    }
)  # fmt: skip
_ALLOWED_ATTRS: Final[dict[str, set[str]]] = {
    "a": {"href", "title"},
    "code": {"class"},
    "pre": {"class"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

# 危险块：整体删除（含内容），不能只去标签 —— 否则脚本正文会漏进上下文
_DANGEROUS_BLOCK_RE: Final = re.compile(
    r"<(script|style|noscript|iframe|object|embed|template|svg)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE: Final = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE: Final = re.compile(r"<[^>]+>")
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE: Final = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_BLANK_LINES_RE: Final = re.compile(r"\n{3,}")
_INLINE_SPACE_RE: Final = re.compile(r"[ \t\u00a0]{2,}")

# 注入句式：只标不删 —— 删了就等于毁掉「网页想指挥模型」的证据
_INJECTION_RE: Final = (
    re.compile(r"忽略(?:以上|之前|前面|上面)[^。\n]{0,10}?(?:指令|提示|要求)", re.IGNORECASE),
    re.compile(
        r"(?:ignore|disregard)\s+(?:all\s+)?(?:previous|above|prior)\s+(?:instructions?|prompts?|rules?)",
        re.IGNORECASE,
    ),
    re.compile(r"you\s+are\s+now\s+(?:a|an|the)\b", re.IGNORECASE),
    re.compile(r"(?:system|developer)\s+prompt", re.IGNORECASE),
    re.compile(r"<\|im_(?:start|end)\|>", re.IGNORECASE),
)
INJECTION_OPEN: Final = "[疑似指令注入，仅作资料]"
INJECTION_CLOSE: Final = "[/疑似指令注入]"


@dataclass(frozen=True, slots=True)
class Material:
    """一段裁剪后的外部内容。"""

    text: str
    suspects: int = 0
    raw_chars: int = 0


def _optional(name: str) -> Any:
    """按需导入可选依赖；没装返回 None（每次调用都走 sys.modules，代价可忽略）。"""
    try:
        return __import__(name)
    except Exception:  # noqa: BLE001 - 依赖缺失不能影响主流程
        return None


def capabilities() -> dict[str, Any]:
    """当前实际生效的净化/抽取实现，供 `/health` 与阶段详情披露。"""
    has_nh3 = _optional("nh3") is not None
    has_trafilatura = _optional("trafilatura") is not None
    degraded = not (has_nh3 and has_trafilatura)
    reason = ""
    if degraded:
        missing = [
            name
            for name, ok in (("nh3", has_nh3), ("trafilatura", has_trafilatura))
            if not ok
        ]
        reason = f"未安装 sanitize extra（缺 {', '.join(missing)}），已退化为标签剥离"
    return {
        "sanitizer": "nh3" if has_nh3 else "regex",
        "extractor": "trafilatura" if has_trafilatura else "regex",
        "degraded": degraded,
        "reason": reason,
    }


def _strip_unsafe(html: str) -> str:
    """兜底净化：删危险块与注释，再去标签。顺序不能反，否则块内容会被当正文留下。"""
    without_blocks = _DANGEROUS_BLOCK_RE.sub(" ", html)
    without_comments = _COMMENT_RE.sub(" ", without_blocks)
    return _TAG_RE.sub(" ", without_comments)


def clean_html(html: str) -> str:
    """HTML 净化：装了 nh3 用白名单清洗，否则退化为危险块剥离。"""
    nh3 = _optional("nh3")
    if nh3 is not None:
        try:
            return str(
                nh3.clean(
                    html,
                    tags=set(_ALLOWED_TAGS),
                    attributes=_ALLOWED_ATTRS,
                    strip_comments=True,
                    link_rel="noopener noreferrer",
                )
            )
        except Exception:  # noqa: BLE001 - 第三方实现异常时退回兜底
            pass
    return _strip_unsafe(html)


def extract_text(html: str, url: str = "") -> str:
    """正文抽取：装了 trafilatura 用真实现，抽不到返回空串由调用方兜底。"""
    trafilatura = _optional("trafilatura")
    if trafilatura is None:
        return ""
    try:
        extracted = trafilatura.extract(
            html,
            url=url or None,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001 - 抽取失败按「没抽到」处理
        return ""
    return str(extracted or "")


def normalize(text: str) -> str:
    """文本归一：去控制符与零宽字符、折叠空白与连续空行。"""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL_RE.sub("", _ZERO_WIDTH_RE.sub("", unified))
    spaced = _INLINE_SPACE_RE.sub(" ", cleaned)
    collapsed = _BLANK_LINES_RE.sub("\n\n", spaced)
    return "\n".join(line.rstrip() for line in collapsed.split("\n")).strip()


def mark_injection(text: str) -> tuple[str, int]:
    """给疑似指令注入的段落加标记（不删除），返回 (新文本, 命中数)。"""
    suspects = 0

    def _mark(match: re.Match[str]) -> str:
        nonlocal suspects
        suspects += 1
        return f"{INJECTION_OPEN}{match.group(0)}{INJECTION_CLOSE}"

    out = text
    for pattern in _INJECTION_RE:
        out = pattern.sub(_mark, out)
    return out, suspects


def clip(text: str, *, limit: int, head_ratio: float = 0.7) -> str:
    """长度裁剪：保留头部与尾部（网页的结论常在尾部），中段标注省略字数。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(0, min(limit, int(limit * head_ratio)))
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    parts = [text[:head]]
    if omitted > 0:
        parts.append(f"\n…（已省略 {omitted} 字）…\n")
    if tail:
        parts.append(text[-tail:])
    return "".join(parts)


def to_text(html: str, url: str = "") -> str:
    """HTML → 可读文本：净化 → 抽取 → 归一；抽不到就退回标签剥离。"""
    cleaned = clean_html(html)
    extracted = extract_text(cleaned, url=url)
    return normalize(extracted or _strip_unsafe(cleaned))


def screen(
    raw: str,
    *,
    is_html: bool,
    limit: int,
    head_ratio: float = 0.7,
    scan: bool = True,
    url: str = "",
) -> Material:
    """对外主入口：把任意外部内容裁成可用材料（失败不抛，返回空文本）。"""
    raw_chars = len(raw)
    text = to_text(raw, url=url) if is_html else normalize(raw)
    suspects = 0
    if scan and text:
        text, suspects = mark_injection(text)
    return Material(
        text=clip(text, limit=limit, head_ratio=head_ratio),
        suspects=suspects,
        raw_chars=raw_chars,
    )
