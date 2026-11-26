"""把引用与附件取成材料块：远程抓取、内容安全裁剪、本地读取。

阶段失败一律降级成该阶段的 status=error / skip，绝不抛异常打断对话：
引用取不到（404、越界、二进制）只损失那一条材料，不损失这一轮。

一条引用有四种结局，必须分开记（见 `Resolution`）：

| 结局 | 含义 | 计入 failed？ | 进 misses？ | 触发降级？ |
|---|---|---|---|---|
| `OK` | 取到且 ≥ `min_chars` | 否 | 否 | 否 |
| `THIN` | 取到了但太短（含正文为空） | 否 | 是（标注「内容不足」） | 是 |
| `SKIPPED` | 没尝试（开关关 / 预算尽 / 已够用） | 否 | 否 | 视 `net` |
| `FAILED` | 尝试后失败 | 是 | 是 | 是 |

把 THIN 并进 FAILED 会把「再搜一次就能补上」的情况说成「不可得」；并进 OK 又会让
模型拿着 40 个字的正文当完整资料。所以它必须是独立一态。
"""

from __future__ import annotations

import os
import time
import urllib.error
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from devops_agent.config import InputPolicy, load_input
from devops_agent.input.attachments import Attachment

from .egress import fetch_text as _fetch_text
from .egress import reason_of

# 门面（pipeline/__init__）从这里取 `_fetch_text` 供测试 monkeypatch；
# 用 `__all__` 声明是「显式再导出」，否则会被当成未使用导入。
__all__ = ["_fetch_text", "_html_to_text"]
from .model import Reference, Stage, _elapsed_ms, _stage
from .sanitize import Material, capabilities, screen, to_text

# 下面三个常量是「默认值」，与 InputPolicy 的字段默认值一致；
# 运行期一律读 config/input.yaml（load_input）。保留常量是为了导出面与对账测试。
MAX_FILE_BYTES: Final = 256 * 1024
FETCH_TIMEOUT_SECONDS: Final = 5.0
FETCH_MAX_BYTES: Final = 512 * 1024
# 「服务端没代为抓取」：与真正的抓取失败区分开——这不是内容不可得，
# 模型用自己的工具仍可能取到，不能替它宣布放弃。
REMOTE_FETCH_OFF: Final = "远程抓取未开启"
# 失败原因保留的字符数：够看清是哪一类错误，又不至于把 detail 撑爆
REASON_LIMIT: Final = 120
# 能安全转成文本的 Content-Type；其余（PDF / 图片 / 二进制）如实说「不是文本」
_TEXTUAL_PREFIXES: Final = ("text/", "application/json", "application/xml", "application/xhtml+xml")
_TEXTUAL_HINTS: Final = ("javascript", "x-www-form-urlencoded")


class Outcome(StrEnum):
    """一条引用的结局。"""

    OK = "ok"
    THIN = "thin"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Resolution:
    """取一条引用的结果：结局 / 内容 / 问题描述 / 规模。"""

    outcome: Outcome
    content: str = ""
    problem: str = ""
    # 裁剪后的字符数与注入命中数：前者判 THIN，后者进阶段详情供人工复核
    chars: int = 0
    suspects: int = 0


def _remote_fetch_enabled(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return os.environ.get("DEVOPS_AGENT_ALLOW_REMOTE_FETCH", "").strip() in {"1", "true", "yes"}


def _clip(text: str, limit: int = REASON_LIMIT) -> str:
    """把失败原因压成一行：detail 要能扫，不该贴整段 traceback。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _html_to_text(html: str) -> str:
    """正文抽取的兼容入口（实现在 sanitize：净化 → 抽取 → 归一）。"""
    return to_text(html)


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
    data = candidate.read_bytes()[: load_input().max_file_bytes]
    if b"\x00" in data[:1024]:
        return "", "二进制文件，跳过"
    return data.decode("utf-8", errors="replace"), ""


def _material_of(raw: str, *, is_html: bool, url: str = "") -> Material:
    """把一段外部内容交给统一安全链（净化 / 归一 / 注入标记 / 裁剪）。"""
    policy = load_input()
    return screen(
        raw,
        is_html=is_html,
        limit=policy.per_url_chars,
        head_ratio=policy.head_ratio,
        scan=policy.injection_scan,
        url=url,
    )


def _resolve_ref(ref: Reference, root: Path | None, remote_fetch: bool) -> Resolution:
    """取一条引用的内容，并给出四态结局（取到 / 太短 / 没尝试 / 失败）。"""
    policy = load_input()
    if ref.kind == "path":
        content, problem = _read_local_file(ref.value, root)
        if problem:
            return Resolution(Outcome.FAILED, problem=problem)
        material = _material_of(content, is_html=False)
        # 本地文件不判 thin：用户自己指的文件多小都算「他给的」，
        # 阈值只用来判断「网页抓到的是不是有效正文」。
        return Resolution(Outcome.OK, content=material.text, chars=len(material.text))
    if ref.kind != "url":
        content = f"（git 引用，未克隆）{ref.value}"
        return Resolution(Outcome.OK, content=content, chars=len(content))
    if not remote_fetch:
        # 没尝试：既不进 failed，也不进 misses（见模块 docstring 的表）
        return Resolution(Outcome.SKIPPED, problem=REMOTE_FETCH_OFF)
    raw, problem = _fetch_quietly(ref.value)
    if problem:
        return Resolution(Outcome.FAILED, problem=problem)
    return _judge(_material_of(raw, is_html=True, url=ref.value), policy)


def _judge(material: Material, policy: InputPolicy) -> Resolution:
    """按 `min_chars` 判 OK / THIN：这是「资料够不够」唯一的判定点。"""
    if len(material.text) < policy.min_chars:
        return Resolution(
            Outcome.THIN,
            content=material.text,
            problem=f"内容不足：{len(material.text)} 字（低于 {policy.min_chars}）",
            chars=len(material.text),
            suspects=material.suspects,
        )
    return Resolution(
        Outcome.OK,
        content=material.text,
        chars=len(material.text),
        suspects=material.suspects,
    )


def _fetch_quietly(url: str) -> tuple[str, str]:
    """抓网页并降级：各类网络错误折成一句**可行动**的原因，不向上抛。"""
    # 运行期从包门面取 _fetch_text：测试会 monkeypatch pipeline._fetch_text，
    # 这里必须读到被替换后的实现，不能绑死在本模块的全局名上。
    from devops_agent.input import pipeline as _pipeline

    policy = load_input()
    start = time.perf_counter()
    try:
        content = _pipeline._fetch_text(url)
    except urllib.error.HTTPError as exc:
        return "", _clip(f"抓取失败：HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        return "", _clip(f"抓取失败：URLError（{reason_of(exc.reason)}）")
    except TimeoutError:
        return "", f"抓取失败：TimeoutError（超过 {policy.fetch_timeout_seconds}s）"
    except ValueError as exc:
        # 地址守卫与 Content-Type 闸都走这条：原因本身就是结论，不必再包一层类名
        return "", _clip(f"抓取失败：{exc}")
    except Exception as exc:  # 兜底：拿不到具体分类时至少留下类名与首句
        return "", _clip(f"抓取失败：{type(exc).__name__}: {exc}")
    took_ms = _elapsed_ms(start)
    if not content.strip():
        return "", f"抓取成功但正文为空（{took_ms}ms）"
    return content, ""


def _join_counts(ok: int, thin: int, failed: int, skipped: int) -> str:
    """把四态计数拼成一行摘要（只为非零项）。"""
    pairs = ((ok, "条成功"), (thin, "条内容不足"), (failed, "条降级"), (skipped, "条跳过"))
    return " · ".join(f"{count} {label}" for count, label in pairs if count)


def _thin_summary(thin: int, failed: int, skipped: int, min_chars: int) -> str:
    """没有可用材料时的摘要：先说「内容不足」，再补失败与跳过。"""
    parts = [f"{thin} 条引用内容不足（低于 {min_chars} 字）"] if thin else []
    if failed:
        parts.append(f"{failed} 条降级")
    if skipped:
        parts.append(f"{skipped} 条跳过")
    return " · ".join(parts) or "无可展开的引用"


def _expand_summary(
    total: int,
    ok: int,
    thin: int,
    failed: int,
    skipped: int,
    *,
    min_chars: int,
    skip_reason: str = "",
) -> tuple[str, str]:
    """引用展开的阶段状态。

    口径：全失败才算 error；全跳过（含远程抓取关闭）算 skip；只要有一条可用就算 ok。
    `thin` 既不是成功也不是失败 —— 摘要里单独说「内容不足」，让「再搜一次」有依据。
    """
    if total == 0:
        return "skip", "无引用可展开"
    if failed == total:
        return "error", f"{failed} 条引用全部未取到"
    if skipped == total:
        # 「没开启」沿用既有文案（前端与文档已引用），其余原因原样透出
        reason = "未开启远程抓取" if skip_reason in ("", REMOTE_FETCH_OFF) else skip_reason
        return "skip", f"{reason}，跳过 {skipped} 条 URL"
    if ok == 0:
        return "skip", _thin_summary(thin, failed, skipped, min_chars)
    return "ok", _join_counts(ok, thin, failed, skipped)


def _attach_summary(total: int, ok: int, failed: int) -> tuple[str, str]:
    """附件读取的阶段状态，口径与引用展开一致（全失败才 error）。"""
    if total == 0:
        return "skip", "未附加文件"
    if failed == total:
        return "error", f"{failed} 份附件全部未取到"
    if failed:
        return "ok", f"{ok} 份附件已读取 · {failed} 份降级"
    return "ok", f"{ok} 份附件已读取"


@dataclass
class _Tally:
    """一轮预抓取的计数：四态 + 累计字数 + 注入命中数 + 首个跳过原因。"""

    ok: int = 0
    thin: int = 0
    failed: int = 0
    skipped: int = 0
    chars: int = 0
    suspects: int = 0
    skip_reason: str = ""


def _budget_skip(policy: InputPolicy, used_ms: int, total_chars: int, ok: int) -> str:
    """预算与「够用」判定：返回跳过原因；空串表示可以继续抓。"""
    if used_ms >= policy.total_ms:
        return "预算用尽（时间）"
    if total_chars >= policy.total_chars:
        return "预算用尽（字符）"
    if total_chars >= policy.min_total_chars and ok:
        return "已够用"
    return ""


def _absorb(
    ref: Reference,
    resolution: Resolution,
    tally: _Tally,
    blocks: list[tuple[Reference, str]],
    notes: list[str],
    misses: list[str],
    thin_refs: list[str],
) -> None:
    """把一条引用的结局记账：四态各自累加，并把可读原因写进 notes。"""
    tally.suspects += resolution.suspects
    if resolution.outcome is Outcome.SKIPPED:
        tally.skipped += 1
        tally.skip_reason = tally.skip_reason or str(resolution.problem)
        notes.append(f"{ref.value} → {resolution.problem}")
        return
    if resolution.outcome is Outcome.FAILED:
        tally.failed += 1
        notes.append(f"{ref.value} → {resolution.problem}")
        misses.append(f"{ref.value} → {resolution.problem}")
        return
    blocks.append((ref, resolution.content))
    tally.chars += resolution.chars
    suspect_note = f"，注入标记 {resolution.suspects}" if resolution.suspects else ""
    if resolution.outcome is Outcome.THIN:
        tally.thin += 1
        thin_refs.append(ref.value)
        notes.append(f"{ref.value} → {resolution.chars} 字{suspect_note}（内容不足）")
        misses.append(f"{ref.value} → {resolution.problem}")
        return
    tally.ok += 1
    notes.append(f"{ref.value} → {resolution.chars} 字{suspect_note}")


def _expand(
    refs: list[Reference], root: Path | None, remote_fetch: bool
) -> tuple[list[tuple[Reference, str]], list[str], list[str], Stage]:
    """把每条引用换成材料块，并按预算「抓到手为止」。

    1) 逐条取，四态分别计数（ok / thin / failed），`thin` 保留内容但记入缺口；
    2) 累计字符达到 `min_total_chars` 且已有可用材料 → 剩余标 skipped(已够用)；
    3) 时间或字符预算用尽 → 剩余标 skipped(预算)，不判 error。

    取不到与内容不足的引用以 `misses` 返回：它们必须进模型上下文，只写进阶段详情
    等于模型不知道自己缺了什么，于是自己去抓同一个地址反复失败。
    """
    start = time.perf_counter()
    policy = load_input()
    blocks: list[tuple[Reference, str]] = []
    notes: list[str] = []
    misses: list[str] = []
    thin_refs: list[str] = []
    tally = _Tally()

    for ref in refs:
        skip = _budget_skip(policy, _elapsed_ms(start), tally.chars, tally.ok)
        if skip:
            tally.skipped += 1
            tally.skip_reason = tally.skip_reason or skip
            suffix = f"（累计 {tally.chars} 字）" if skip == "已够用" else ""
            notes.append(f"{ref.value} → {skip}{suffix}")
            continue
        _absorb(ref, _resolve_ref(ref, root, remote_fetch), tally, blocks, notes, misses, thin_refs)

    status, summary = _expand_summary(
        len(refs),
        tally.ok,
        tally.thin,
        tally.failed,
        tally.skipped,
        min_chars=policy.min_chars,
        skip_reason=tally.skip_reason,
    )
    if tally.suspects:
        notes.append(f"疑似指令注入 {tally.suspects} 处，已标记为不可信资料")
    caps = capabilities()
    if caps["degraded"]:
        # 缺 [sanitize] extra 时说清楚：否则「正文抽得又短又乱」会被当成站点问题
        notes.append(str(caps["reason"]))
    added = sum(len(content) for _, content in blocks)
    return (
        blocks,
        misses,
        thin_refs,
        _stage(
            "expand",
            status=status,
            summary=summary,
            # before 记「被展开的引用原文总长」：与 after（材料字数）同轴才有意义
            before=sum(len(ref.value) for ref in refs),
            after=added,
            items=len(refs),
            ms=_elapsed_ms(start),
            detail="\n".join(notes),
            changed=bool(blocks),
            failed=tally.failed,
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
