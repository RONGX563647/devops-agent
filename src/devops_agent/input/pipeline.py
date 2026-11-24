"""输入解析管线 —— 把用户原始输入整理成可安全交给模型的上下文。

设计取舍：
- 纯 stdlib、不 import 任何框架：网关（main.py）与测试都能直接用；
- 每个阶段都产出一条 Stage 记录（before / after / 耗时 / 是否真的改动），
  网关按记录发事件并落库，前端据此把「这条输入被做了什么」画成轨迹；
- 阶段失败一律降级成该阶段的 status=error / skip，绝不抛异常打断对话：
  引用取不到（404、越界、二进制）只损失那一条材料，不损失这一轮。
- 「是否真的改动了输入」由 changed 表达：数据库全量保留（审计需要），
  界面只呈现 changed 或出错的阶段，避免一屏「无需处理」。

依赖策略（AGENTS.md §3.4：能力用内置、不重复造轮子）：
本地文件读取只用 stdlib；网页正文抽取优先用 trafilatura，未安装时退化为
标签剥离的兜底实现；远程抓取默认关闭，需 DEVOPS_AGENT_ALLOW_REMOTE_FETCH=1 显式开启。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from devops_agent.input.attachments import Attachment

# 单次输入的总字符预算（正文 + 全部材料）：超出后按「先去噪、再取舍」处理
MAX_TOTAL_CHARS: Final = 24_000
# 预算里留给材料的份额：材料再多也不能把用户原话挤没
MATERIAL_SHARE: Final = 0.65
# 单份材料裁剪后的下限：留 200 字通常还能看出「这是什么文件」
MIN_MATERIAL_CHARS: Final = 200
# 单个本地文件最多读取的字节数（超出部分截断，并在阶段 detail 里写明）
MAX_FILE_BYTES: Final = 256 * 1024
# 单条输入里最多解析的引用条数，避免一条消息挂 50 个链接把预算吃光
MAX_REFERENCES: Final = 5
# fetch 超时与响应体上限
FETCH_TIMEOUT_SECONDS: Final = 5.0
FETCH_MAX_BYTES: Final = 512 * 1024
# token 估算口径：与 AgentScope formatter 的「字节数 / 4」同源，按字符算更保守
CHARS_PER_TOKEN: Final = 4

STAGE_LABELS: Final[dict[str, str]] = {
    "normalize": "归一化",
    "detect": "引用探测",
    "expand": "引用展开",
    "attach": "附件读取",
    "redact": "密钥脱敏",
    "budget": "预算裁剪",
    "compose": "组装",
}

KIND_LABELS: Final[dict[str, str]] = {
    "url": "URL",
    "path": "本地路径",
    "git": "git 引用",
    "file": "附件",
}

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
# 占位符语法：与 secrets/vault.py 的 PLACEHOLDER_PATTERN 必须逐字一致，
# 两边由 tests/unit/test_secrets.py 里的一条对账用例钉住，改一处就会红。
_PLACEHOLDER_RE: Final = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
# 脱敏中途的临时槽位：用私用区字符，任何密钥正则都不会命中它
_SLOT_START: Final = "\ue000"
_SLOT_END: Final = "\ue001"
_SLOT_RE: Final = re.compile(_SLOT_START + r"(\d+)" + _SLOT_END)
_BASE64_RE: Final = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")

# 密钥识别规则：命中后整段替换成 {{secret:name}}，指纹只留前 4 位与长度
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
)
_ASSIGNED_SECRET_RE: Final = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd)\b(\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9_\-.]{12,})"
)

# 凭据落库回调：网关传进来（接保险箱），管线本身不认识存储
SecretSink = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class Stage:
    """一个解析阶段的执行结果（前端轨迹的一行）。"""

    stage: str
    label: str
    status: str  # ok / skip / error
    summary: str
    before: int
    after: int
    items: int
    ms: int
    detail: str
    changed: bool
    failed: int = 0  # 该阶段内部失败的条目数（部分降级）


@dataclass(frozen=True, slots=True)
class Reference:
    """从输入里识别出的一条引用。"""

    kind: str  # url / path / git / file
    value: str


@dataclass(frozen=True, slots=True)
class PreparedInput:
    """解析结果：交给模型的最终文本 + 全过程记录。"""

    original: str
    text: str
    stages: tuple[Stage, ...]
    references: tuple[Reference, ...]
    redactions: int

    @property
    def degraded(self) -> int:
        """出错（非 skip）的阶段数，供轨迹标「降级」徽标。"""
        return sum(1 for stage in self.stages if stage.status == "error" or stage.failed)

    @property
    def effective(self) -> tuple[Stage, ...]:
        """真正改动了输入的阶段——界面只展示这些。"""
        return tuple(stage for stage in self.stages if stage.changed or stage.status == "error")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算，用于在轨迹里与模型回报的 input_tokens 对账。"""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _stage(
    name: str,
    *,
    status: str,
    summary: str,
    before: int,
    after: int,
    changed: bool,
    items: int = 0,
    ms: int = 0,
    detail: str = "",
    failed: int = 0,
) -> Stage:
    return Stage(
        stage=name,
        label=STAGE_LABELS[name],
        status=status,
        summary=summary,
        before=before,
        after=after,
        items=items,
        ms=ms,
        detail=detail,
        changed=changed,
        failed=failed,
    )


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
        return "", "远程抓取未开启"
    return _fetch_quietly(ref.value)


def _fetch_quietly(url: str) -> tuple[str, str]:
    """抓网页并降级：网络错误一律折成一句可读的原因，不向上抛。"""
    try:
        content = _fetch_text(url)
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
) -> tuple[list[tuple[Reference, str]], Stage]:
    """把每条引用换成可直接塞进上下文的材料块。"""
    start = time.perf_counter()
    blocks: list[tuple[Reference, str]] = []
    notes: list[str] = []
    failures = 0

    for ref in refs:
        content, problem = _resolve_ref(ref, root, remote_fetch)
        if problem:
            failures += 1
            notes.append(f"{ref.value} → {problem}")
            continue
        blocks.append((ref, content))
        notes.append(f"{ref.value} → {len(content)} 字")

    status, summary = _expand_summary(len(refs), len(blocks), failures)
    added = sum(len(content) for _, content in blocks)
    return blocks, _stage(
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
    )


def _attach(attachments: Sequence[Attachment]) -> tuple[list[tuple[Reference, str]], Stage]:
    """把上传的附件变成材料块——正文在入库时已抽好，这里不再碰磁盘。"""
    start = time.perf_counter()
    blocks: list[tuple[Reference, str]] = []
    notes: list[str] = []
    failures = 0

    for attachment in attachments:
        if not attachment.ok:
            failures += 1
            notes.append(f"{attachment.name} → {attachment.problem}")
            continue
        blocks.append((Reference(kind="file", value=attachment.name), attachment.text))
        suffix = "（已截断）" if attachment.truncated else ""
        notes.append(f"{attachment.name} → {attachment.chars} 字{suffix}")

    status, summary = _attach_summary(len(attachments), len(blocks), failures)
    added = sum(len(content) for _, content in blocks)
    return blocks, _stage(
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
    )


class _SecretNamer:
    """给同一类凭据分配互不冲突的占位符名。

    同一个名字下出现两个不同的值（比如两把 OpenAI key）时，第二个起编号，
    否则两个不同的密钥会共用一个占位符，注入时必然张冠李戴。
    """

    def __init__(self) -> None:
        self._named: dict[tuple[str, str], str] = {}
        self._taken: set[str] = set()

    def name_for(self, base: str, value: str) -> str:
        """同一个值永远拿到同一个名字；不同值按出现顺序编号。"""
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        existing = self._named.get((base, digest))
        if existing is not None:
            return existing
        name = base
        index = 2
        while name in self._taken:
            name = f"{base}_{index}"
            index += 1
        self._named[(base, digest)] = name
        self._taken.add(name)
        return name


def _fingerprint(secret: str) -> str:
    """只暴露前 4 位与长度：足够认出是哪个凭据，又不构成泄漏。"""
    return f"{secret[:4]}…（{len(secret)} 位）"


def _protect_placeholders(text: str) -> tuple[str, list[str]]:
    """把已有的占位符挪到临时槽位，避免后续正则把它当成「密钥赋值」再脱一次。

    为什么必须保护：`key=...` 那条规则会把 `{{secret:aws_access_key}}` 里的
    `secret:aws_access_key` 当成一次赋值，产出一层套一层的
    `{{secret:{{secret:secret}}}}`——占位符名字一长（≥12 字符）就会踩到。
    """
    slots: list[str] = []

    def _take(match: re.Match[str]) -> str:
        slots.append(match.group(0))
        return f"{_SLOT_START}{len(slots) - 1}{_SLOT_END}"

    return _PLACEHOLDER_RE.sub(_take, text), slots


def _restore_placeholders(text: str, slots: list[str]) -> str:
    """把临时槽位还原成占位符。"""
    if not slots:
        return text
    return _SLOT_RE.sub(lambda match: slots[int(match.group(1))], text)


def _apply_typed_patterns(
    text: str, namer: _SecretNamer, sink: SecretSink | None, notes: list[str]
) -> str:
    """按已知凭据前缀替换（AWS / OpenAI / GitHub …）。"""
    out = text
    for pattern_name, pattern in _SECRET_PATTERNS:

        def _replace(match: re.Match[str], pattern_name: str = pattern_name) -> str:
            value = match.group(0)
            name = namer.name_for(pattern_name, value)
            if sink is not None:
                sink(name, value)
            notes.append(f"{name} · {_fingerprint(value)} → {{{{secret:{name}}}}}")
            return f"{{{{secret:{name}}}}}"

        out = pattern.sub(_replace, out)
    return out


def _apply_assigned_pattern(
    text: str, namer: _SecretNamer, sink: SecretSink | None, notes: list[str]
) -> str:
    """替换 `api_key=...` 这类显式赋值（保留键名，只换值）。"""

    def _replace(match: re.Match[str]) -> str:
        base = match.group(1).lower().replace("-", "_")
        value = match.group(3)
        name = namer.name_for(base, value)
        if sink is not None:
            sink(name, value)
        notes.append(f"{name} · {_fingerprint(value)} → {{{{secret:{name}}}}}")
        return f"{match.group(1)}{match.group(2)}{{{{secret:{name}}}}}"

    return _ASSIGNED_SECRET_RE.sub(_replace, text)


def _redact_text(text: str, namer: _SecretNamer, sink: SecretSink | None) -> tuple[str, list[str]]:
    """把命中的密钥替换成 {{secret:name}}，返回替换记录。

    两段式替换：先按凭据前缀，再处理显式赋值。中间用临时槽位把已生成的
    占位符保护起来——否则赋值规则会把 `{{secret:aws_access_key}}` 里的
    `secret:aws_access_key` 当成一次赋值，套成 `{{secret:{{secret:secret}}}}`。
    """
    notes: list[str] = []
    typed = _apply_typed_patterns(text, namer, sink, notes)
    protected, slots = _protect_placeholders(typed)
    out = _apply_assigned_pattern(protected, namer, sink, notes)
    return _restore_placeholders(out, slots), notes


def _redact(
    text: str,
    blocks: list[tuple[Reference, str]],
    namer: _SecretNamer,
    sink: SecretSink | None,
) -> tuple[str, list[tuple[Reference, str]], Stage]:
    """对用户正文与全部材料同时脱敏——任何一条都不该把密钥送进模型。"""
    start = time.perf_counter()
    before = len(text)
    clean, notes = _redact_text(text, namer, sink)
    clean_blocks: list[tuple[Reference, str]] = []
    for ref, content in blocks:
        clean_content, block_notes = _redact_text(content, namer, sink)
        notes.extend(f"{ref.value}: {note}" for note in block_notes)
        clean_blocks.append((ref, clean_content))
    changed = bool(notes)
    return (
        clean,
        clean_blocks,
        _stage(
            "redact",
            status="ok" if changed else "skip",
            summary=f"{len(notes)} 处凭据已加密入库并替换为占位符" if notes else "未发现凭据",
            before=before,
            after=len(clean),
            items=len(notes),
            ms=_elapsed_ms(start),
            detail="\n".join(notes),
            changed=changed,
        ),
    )


def _dedupe_lines(text: str) -> tuple[str, int]:
    """折叠连续重复行（日志里最常见的噪声），返回 (文本, 折叠行数)。"""
    out: list[str] = []
    removed = 0
    previous: str | None = None
    repeat = 0
    for line in text.split("\n"):
        if line and line == previous:
            repeat += 1
            removed += 1
            continue
        if repeat:
            out.append(f"…（重复 {repeat} 行）")
            repeat = 0
        out.append(line)
        previous = line
    if repeat:
        out.append(f"…（重复 {repeat} 行）")
    return "\n".join(out), removed


def _denoise(text: str) -> tuple[str, list[str]]:
    """去噪：移除 base64 块、折叠连续重复行。"""
    notes: list[str] = []
    work = text
    b64 = _BASE64_RE.findall(work)
    if b64:
        work = _BASE64_RE.sub("[已移除 base64 块]", work)
        notes.append(f"移除 base64 块 ×{len(b64)}")
    work, removed = _dedupe_lines(work)
    if removed:
        notes.append(f"折叠重复行 ×{removed}")
    return work, notes


def _truncate(text: str, limit: int) -> str:
    """中段截断：保留首尾，中间留一行说明，避免只剩开头看不出结论。"""
    if len(text) <= limit:
        return text
    marker = f"\n\n…（已截断 {len(text) - limit} 字）…\n\n"
    budget = max(0, limit - len(marker))
    head = int(budget * 0.6)
    tail = budget - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"


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
    limit = MAX_TOTAL_CHARS if limit is None else limit
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


def _compose(text: str, blocks: list[tuple[Reference, str]]) -> tuple[str, Stage]:
    """拼出最终交给模型的文本：用户原话 + 材料附录。"""
    start = time.perf_counter()
    before = len(text)
    if not blocks:
        return text, _stage(
            "compose",
            status="skip",
            summary="原样透传",
            before=before,
            after=before,
            ms=_elapsed_ms(start),
            changed=False,
        )
    appendix = "\n\n".join(
        f"### 引用材料 {index} · {ref.value}\n\n```\n{content}\n```"
        for index, (ref, content) in enumerate(blocks, start=1)
    )
    merged = f"{text}\n\n---\n\n{appendix}"
    tokens = estimate_tokens(merged)
    return merged, _stage(
        "compose",
        status="ok",
        summary=f"追加 {len(blocks)} 份材料 · 最终 {len(merged)} 字 ≈ {tokens} token",
        before=before,
        after=len(merged),
        items=len(blocks),
        ms=_elapsed_ms(start),
        changed=True,
    )


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
    blocks, expand_stage = _expand(refs, root, _remote_fetch_enabled(remote_fetch))
    attached, attach_stage = _attach(attachments)
    text, all_blocks, redact_stage = _redact(text, blocks + attached, namer, on_secret)
    text, all_blocks, budget_stage = _budget(text, all_blocks)
    text, compose_stage = _compose(text, all_blocks)

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
    )
