"""引用候选识别：URL / 本地路径 / git 引用。

顺序固定为 git → url → path：先认 .git 仓库，网址才不会把仓库地址当网页，
路径再兜底。超过 MAX_REFERENCES 的部分直接丢弃，避免一条消息把预算吃光。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import Final

from .model import KIND_LABELS, Reference, Stage, _elapsed_ms, _stage

# 单条输入里最多解析的引用条数，避免一条消息挂 50 个链接把预算吃光
MAX_REFERENCES: Final = 5

# 引用识别：git 必须先于 url 匹配，否则 xxx.git 会被当成普通网址
_GIT_RE: Final = re.compile(
    r"(?:git@[\w.\-]+:[\w.\-/]+(?:\.git)?"
    r"|git\+(?:https?|ssh)://[^\s<>\"']+"
    r"|https?://[^\s<>\"']+\.git)"
)
_URL_RE: Final = re.compile(r"https?://[^\s<>\"'）)\]]+")
# 显式路径前缀（./ ../ / ~/ C:\）；裸文件名单独判断，必须真实存在于工作区才算引用
_PATH_RE: Final = re.compile(
    # 前置断言排除 URL 与已识别前缀里的片段：https://a.com/x 不该被切成 /a.com/x
    r"(?<![A-Za-z0-9_:/.])"
    r"(?:(?:\.{1,2}/|~/|/|[A-Za-z]:[\\/])[\w\-.@/\\]+"
    r"|[\w\-]+(?:\.[A-Za-z0-9]{1,8}){1,2})"
)
_BARE_NAME_RE: Final = re.compile(r"^[\w\-]+(?:\.[A-Za-z0-9]{1,8}){1,2}$")


def _looks_like_explicit_path(token: str) -> bool:
    return token.startswith(("./", "../", "/", "~/")) or bool(re.match(r"^[A-Za-z]:[\\/]", token))


def _url_candidates(text: str) -> list[str]:
    """网址候选：跳过 .git 结尾（前面已按 git 引用登记）并去掉句末标点。"""
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:，。；：")
        if url.endswith(".git") or url in urls:
            continue
        urls.append(url)
    return urls


def _path_candidates(text: str, root: Path | None) -> list[str]:
    """路径候选：显式前缀直接认；裸文件名必须真实存在于工作区，否则当普通单词。"""
    found: list[str] = []
    for match in _PATH_RE.finditer(text):
        token = match.group(0)
        if "://" in token:
            continue
        exists = root is not None and _BARE_NAME_RE.match(token) and (root / token).is_file()
        if _looks_like_explicit_path(token) or exists:
            found.append(token)
    return found


def _candidates(text: str, root: Path | None) -> list[tuple[str, str]]:
    """按 git → url → path 的顺序给出候选，后面按首个命中去重。"""
    items: list[tuple[str, str]] = []
    for match in _GIT_RE.finditer(text):
        items.append(("git", match.group(0)))
    for url in _url_candidates(text):
        items.append(("url", url))
    for token in _path_candidates(text, root):
        items.append(("path", token))
    return items


def _ref_summary(refs: list[Reference]) -> tuple[str, str]:
    """一行摘要（按类型计数）+ 展开清单（逐条）。"""
    kinds = Counter(ref.kind for ref in refs)
    summary = " · ".join(f"{KIND_LABELS[kind]} ×{count}" for kind, count in kinds.items())
    detail = "\n".join(f"{KIND_LABELS[ref.kind]} · {ref.value}" for ref in refs)
    return summary, detail


def _detect(text: str, root: Path | None) -> tuple[list[Reference], Stage]:
    """识别输入里的 URL / 本地路径 / git 引用（只登记，不读取）。

    顺序固定为 git → url → path：先认 .git 仓库，网址才不会把仓库地址当网页，
    路径再兜底。超过 MAX_REFERENCES 的部分直接丢弃，避免一条消息把预算吃光。
    """
    start = time.perf_counter()
    refs: list[Reference] = []
    seen: set[str] = set()
    for kind, value in _candidates(text, root):
        if len(refs) >= MAX_REFERENCES or value in seen:
            continue
        seen.add(value)
        refs.append(Reference(kind=kind, value=value))

    summary, detail = _ref_summary(refs)
    return refs, _stage(
        "detect",
        status="ok" if refs else "skip",
        summary=summary or "未发现可解析引用",
        before=len(text),
        after=len(text),
        items=len(refs),
        ms=_elapsed_ms(start),
        detail=detail,
        changed=bool(refs),
    )
