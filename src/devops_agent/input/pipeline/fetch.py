"""把引用与附件取成材料块：远程抓取、HTML 转文本、本地读取。

阶段失败一律降级成该阶段的 status=error / skip，绝不抛异常打断对话：
引用取不到（404、越界、二进制）只损失那一条材料，不损失这一轮。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from devops_agent.input.attachments import Attachment

from .model import Reference, Stage, _elapsed_ms, _stage

# 单个本地文件最多读取的字节数（超出部分截断，并在阶段 detail 里写明）
MAX_FILE_BYTES: Final = 256 * 1024
# fetch 超时与响应体上限
FETCH_TIMEOUT_SECONDS: Final = 5.0
FETCH_MAX_BYTES: Final = 512 * 1024
# 「服务端没代为抓取」：与真正的抓取失败区分开——这不是内容不可得，
# 模型用自己的工具仍可能取到，不能替它宣布放弃。
REMOTE_FETCH_OFF: Final = "远程抓取未开启"


def _remote_fetch_enabled(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return os.environ.get("DEVOPS_AGENT_ALLOW_REMOTE_FETCH", "").strip() in {"1", "true", "yes"}


def _html_to_text(html: str) -> str:
    """正文抽取：优先 trafilatura，未安装时退化为标签剥离。"""
    try:
        import trafilatura  # type: ignore[import-not-found]
    except ImportError:
        return re.sub(r"<[^>]+>", " ", html)
    extracted = trafilatura.extract(html)
    return extracted if extracted else re.sub(r"<[^>]+>", " ", html)


def _fetch_text(url: str) -> str:
    """抓取一个网址的正文（stdlib，超时与体积双限）。"""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "devops-agent/0.1"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310
        raw = resp.read(FETCH_MAX_BYTES)
    return _html_to_text(raw.decode("utf-8", errors="replace"))


def _read_local_file(token: str, root: Path | None) -> tuple[str, str]:
    """读取本地文件，返回 (内容, 问题描述)；问题描述非空表示这条引用没取到。"""
    if root is None:
        return "", "未配置工作区根目录，跳过本地文件"
    base = root.resolve()
    candidate = (base / token).resolve() if not Path(token).is_absolute() else Path(token).resolve()
    if not candidate.is_relative_to(base):
        return "", "路径越界，已拒绝读取"
    if not candidate.is_file():
        return "", "文件不存在"
    data = candidate.read_bytes()[:MAX_FILE_BYTES]
    if b"\x00" in data[:1024]:
        return "", "二进制文件，跳过"
    return data.decode("utf-8", errors="replace"), ""


def _resolve_ref(ref: Reference, root: Path | None, remote_fetch: bool) -> tuple[str, str]:
    """取一条引用的内容，返回 (内容, 问题)；问题非空表示这条没取到。"""
    if ref.kind == "path":
        return _read_local_file(ref.value, root)
    if ref.kind != "url":
        return f"（git 引用，未克隆）{ref.value}", ""
    if not remote_fetch:
        return "", REMOTE_FETCH_OFF
    return _fetch_quietly(ref.value)


def _fetch_quietly(url: str) -> tuple[str, str]:
    """抓网页并降级：网络错误一律折成一句可读的原因，不向上抛。"""
    # 运行期从包门面取 _fetch_text：测试会 monkeypatch pipeline._fetch_text，
    # 这里必须读到被替换后的实现，不能绑死在本模块的全局名上。
    from devops_agent.input import pipeline as _pipeline

    try:
        content = _pipeline._fetch_text(url)
    except Exception as exc:  # 网络错误种类太多，统一降级成一条失败记录
        return "", f"抓取失败：{type(exc).__name__}"
    return (content, "") if content.strip() else ("", "正文为空")


def _expand_summary(total: int, ok: int, failed: int) -> tuple[str, str]:
    """引用展开的阶段状态：全失败才算 error，部分失败仍算 ok 但要记 failed。"""
    if total == 0:
        return "skip", "无引用可展开"
    if failed == total:
        return "error", f"{failed} 条引用全部未取到"
    if failed:
        return "ok", f"{ok} 条成功 · {failed} 条降级"
    return "ok", f"{ok} 条引用已展开"


def _attach_summary(total: int, ok: int, failed: int) -> tuple[str, str]:
    """附件读取的阶段状态，口径与引用展开一致（全失败才 error）。"""
    if total == 0:
        return "skip", "未附加文件"
    if failed == total:
        return "error", f"{failed} 份附件全部未取到"
    if failed:
        return "ok", f"{ok} 份附件已读取 · {failed} 份降级"
    return "ok", f"{ok} 份附件已读取"


def _expand(
    refs: list[Reference], root: Path | None, remote_fetch: bool
) -> tuple[list[tuple[Reference, str]], list[str], Stage]:
    """把每条引用换成可直接塞进上下文的材料块。

    取不到的引用同时以 `misses` 返回：它必须进模型上下文，只写进阶段详情
    等于模型不知道自己手里缺了什么，于是自己去抓同一个地址反复失败。
    """
    start = time.perf_counter()
    blocks: list[tuple[Reference, str]] = []
    notes: list[str] = []
    misses: list[str] = []
    failures = 0

    for ref in refs:
        content, problem = _resolve_ref(ref, root, remote_fetch)
        if problem:
            failures += 1
            notes.append(f"{ref.value} → {problem}")
            # 「远程抓取未开启」只是服务端没代为抓取，不写进 misses：
            # 写进去等于替模型判定「内容不可得」，会劝退它用 shell 自己取。
            if problem != REMOTE_FETCH_OFF:
                misses.append(f"{ref.value} → {problem}")
            continue
        blocks.append((ref, content))
        notes.append(f"{ref.value} → {len(content)} 字")

    status, summary = _expand_summary(len(refs), len(blocks), failures)
    added = sum(len(content) for _, content in blocks)
    return (
        blocks,
        misses,
        _stage(
            "expand",
            status=status,
            summary=summary,
            before=0,
            after=added,
            items=len(refs),
            ms=_elapsed_ms(start),
            detail="\n".join(notes),
            changed=bool(blocks),
            failed=failures,
        ),
    )


def _attach(
    attachments: Sequence[Attachment],
) -> tuple[list[tuple[Reference, str]], list[str], Stage]:
    """把上传的附件变成材料块——正文在入库时已抽好，这里不再碰磁盘。"""
    start = time.perf_counter()
    blocks: list[tuple[Reference, str]] = []
    notes: list[str] = []
    misses: list[str] = []
    failures = 0

    for attachment in attachments:
        if not attachment.ok:
            failures += 1
            notes.append(f"{attachment.name} → {attachment.problem}")
            misses.append(f"{attachment.name} → {attachment.problem}")
            continue
        blocks.append((Reference(kind="file", value=attachment.name), attachment.text))
        suffix = "（已截断）" if attachment.truncated else ""
        notes.append(f"{attachment.name} → {attachment.chars} 字{suffix}")

    status, summary = _attach_summary(len(attachments), len(blocks), failures)
    added = sum(len(content) for _, content in blocks)
    return (
        blocks,
        misses,
        _stage(
            "attach",
            status=status,
            summary=summary,
            before=0,
            after=added,
            items=len(attachments),
            ms=_elapsed_ms(start),
            detail="\n".join(notes),
            changed=bool(blocks),
            failed=failures,
        ),
    )
