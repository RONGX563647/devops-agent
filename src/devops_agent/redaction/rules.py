"""脱敏规则单源 —— 输入侧、输出侧与代码上下文共用一套「什么是凭据」的判定。

为什么必须单源：这份约定曾有两份实现（`input/pipeline/redact.py` 与
`secrets/capture.py`），`sk-` 最短长度、`Bearer` 支持、键名前缀三处已经漂移。
两份表各自演进的结果是误报与漏报朝相反方向走，且修一处漏一处。

本模块只依赖标准库：不 import 框架、不 import 任何一个消费方，因此
`input` / `secrets` / `memory` 都可以安全指向它。

判定分四道闸，代价递增，任一不过就放行：

1. 形状闸：值必须像随机串（字母 + 数字），不能是 Python 标识符或点分模块名；
2. 位置闸：赋值分隔符只允许行内空白，禁止跨行吞掉下一条语句；
3. 上下文闸：熵兜底要求「凭据词处于赋值位置」，且凭据词是独立词；
4. 档案闸：`profile="code"`（读源码）关闭熵兜底，只保留已知前缀与赋值式。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

# --- 档案 ---------------------------------------------------------------
PROFILE_INPUT: Final = "input"
PROFILE_OUTPUT: Final = "output"
PROFILE_CODE: Final = "code"

# --- 置信档 -------------------------------------------------------------
CONFIDENCE_KNOWN: Final = "known_prefix"
CONFIDENCE_ASSIGNED: Final = "assigned"
CONFIDENCE_ENTROPY: Final = "entropy"
_RANK: Final = {CONFIDENCE_KNOWN: 0, CONFIDENCE_ASSIGNED: 1, CONFIDENCE_ENTROPY: 2}

# 占位符语法：与 secrets/vault.py 的 PLACEHOLDER_PATTERN 必须逐字一致，
# 两边由 tests/unit/test_secrets.py 里的一条对账用例钉住，改一处就会红。
PLACEHOLDER_PATTERN: Final = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")

# 脱敏中途的临时槽位：用私用区字符，任何密钥正则都不会命中它
_SLOT_START: Final = "\ue000"
_SLOT_END: Final = "\ue001"
_SLOT_RE: Final = re.compile(_SLOT_START + r"(\d+)" + _SLOT_END)

# 已知凭据前缀：这些形状不会被正常代码/日志偶然产出，因此不需要再过形状闸
_KNOWN_PREFIXES: Final = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
)

# 赋值式：`api_key=...` / `DB_PASSWORD: ...`。分隔符只吃行内空白 —— 用 `\s*`
# 会跨行把下一条语句（`continue`）当成值替换掉，`detect.py` 上真实踩过。
_ASSIGNED_RE: Final = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key|secret|token|password|passwd))"
    r"([ \t]{0,4}[:=][ \t]{0,4}[\"']?)"
    r"([A-Za-z0-9_\-.+/=]{8,})"
)
# Bearer 不是赋值语法，单独保留前缀，避免把 "Bearer " 一起吞进占位符
_BEARER_RE: Final = re.compile(r"(?i)\b(Bearer)([ \t]+)([A-Za-z0-9._\-]{10,})")

# 高熵兜底的候选词：只认「连续 20+ 的 token 形状」，逐词判定而不是整段
_ENTROPY_CANDIDATE_RE: Final = re.compile(r"[A-Za-z0-9_\-+/=.]{20,}")
# 凭据词必须独立（前面不能是下划线，否则 `estimate_prefix_tokens` 会被算进来），
# 且必须处于赋值位置或 Bearer 之后 —— 同段正文里提到 token 不算证据
_HINT_POSITION_RE: Final = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(?:api[_-]?key|access[_-]?key|secret|token|password|passwd)"
    r"(?![A-Za-z0-9_])[ \t]{0,4}[:=]"
    r"|\bBearer[ \t]"
)

_ENTROPY_MIN: Final = 3.5
# 无数字的混合大小写串要更高熵才收：这条路径没有「数字」这个强证据
_ENTROPY_STRICT: Final = 3.8
_VALUE_MIN: Final = 8
_VALUE_MAX: Final = 2048

# 点分标识符 / 单词标识符：代码里最常见、也最不该被当成凭据的形状。
# 只认「全小写」——`Tr0ub4dor3xKq9mZpL2nQ` 这类混合大小写的随机串必须放过；
# `estimate_prefix_tokens`、`devops_agent.memory.tokens` 才是要拦的。
_IDENTIFIER_RE: Final = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\Z")
_KEYWORDS: Final = frozenset(
    {
        "continue",
        "break",
        "return",
        "pass",
        "class",
        "import",
        "from",
        "else",
        "elif",
        "raise",
        "yield",
        "async",
        "await",
        "lambda",
        "global",
        "assert",
        "while",
        "with",
        "try",
        "finally",
        "except",
    }
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """一处候选命中：`value` 是要替换的原文，`[start, end)` 是它在文本里的跨度。"""

    base: str
    value: str
    start: int
    end: int
    confidence: str


@dataclass(slots=True)
class Applied:
    """一次脱敏的实际结果：同一 (base, value) 只出现一条，`count` 记命中次数。"""

    base: str
    name: str
    value: str
    confidence: str
    count: int = 0


NameFor = Callable[[str, str], str]
Sink = Callable[[str, str], None]


def shannon_entropy(value: str) -> float:
    """字符分布的 Shannon 熵：纯数字/重复串低，随机 token 高。"""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def shape_ok(value: str, confidence: str) -> bool:
    """形状闸：像随机串才算值；标识符、关键字、纯单词一律放行。"""
    if not _VALUE_MIN <= len(value) <= _VALUE_MAX:
        return False
    if confidence == CONFIDENCE_KNOWN:
        return True
    # 标识符只有在「不含数字」时才算误报形状：`estimate_prefix_tokens`、
    # `devops_agent.memory.tokens` 都没有数字；而 `abcdefghij1234567890`
    # 这类小写 alnum 串恰恰是常见 API key，必须放过。
    has_digit = any(char.isdigit() for char in value)
    if _IDENTIFIER_RE.match(value) and not has_digit:
        return False
    if value.lower() in _KEYWORDS:
        return False
    if not any(char.isalpha() for char in value):
        return False
    # 首选判据：字母 + 数字同时出现（随机 token 的绝大多数）。
    if has_digit:
        return True
    # 次选判据：够长、大小写混合、熵足够（覆盖无数字的 base64 串）。
    # 全大写/全小写的单词与常量（DEVOPS_AGENT_API_KEY）不满足「大小写混合」。
    has_lower = any(char.islower() for char in value)
    has_upper = any(char.isupper() for char in value)
    return (
        has_lower and has_upper and len(value) >= 20 and shannon_entropy(value) >= _ENTROPY_STRICT
    )


def _base_of(key: str) -> str:
    return key.lower().replace("-", "_")


def _protect_placeholders(text: str) -> tuple[str, list[str]]:
    """把已有占位符挪到临时槽位，避免后续规则把它当成凭据再脱一层。"""
    slots: list[str] = []

    def _take(match: re.Match[str]) -> str:
        slots.append(match.group(0))
        return f"{_SLOT_START}{len(slots) - 1}{_SLOT_END}"

    return PLACEHOLDER_PATTERN.sub(_take, text), slots


def _restore_placeholders(text: str, slots: list[str]) -> str:
    if not slots:
        return text
    return _SLOT_RE.sub(lambda match: slots[int(match.group(1))], text)


def _entropy_candidates(text: str) -> list[Candidate]:
    """熵兜底：候选词 + 同行赋值位置 + 独立凭据词，三条同时成立才收。"""
    found: list[Candidate] = []
    for match in _ENTROPY_CANDIDATE_RE.finditer(text):
        value = match.group(0)
        if not shape_ok(value, CONFIDENCE_ENTROPY):
            continue
        if shannon_entropy(value) < _ENTROPY_MIN:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start : line_end if line_end >= 0 else len(text)]
        if not _HINT_POSITION_RE.search(line):
            continue
        found.append(Candidate("entropy", value, match.start(), match.end(), CONFIDENCE_ENTROPY))
    return found


def _prune(candidates: list[Candidate]) -> list[Candidate]:
    """按置信度从高到低收跨度不重叠的候选：高置信命中覆盖低置信的同一段。"""
    kept: list[Candidate] = []
    for item in sorted(candidates, key=lambda cand: (_RANK[cand.confidence], cand.start)):
        if any(not (item.end <= other.start or item.start >= other.end) for other in kept):
            continue
        kept.append(item)
    return sorted(kept, key=lambda cand: cand.start)


def find_candidates(text: str, *, profile: str = PROFILE_OUTPUT) -> list[Candidate]:
    """扫出一段文本里的全部候选凭据；纯函数，不改文本、不落库。"""
    found: list[Candidate] = []
    for name, pattern in _KNOWN_PREFIXES:
        for match in pattern.finditer(text):
            found.append(
                Candidate(name, match.group(0), match.start(), match.end(), CONFIDENCE_KNOWN)
            )
    for match in _ASSIGNED_RE.finditer(text):
        value = match.group(3)
        if shape_ok(value, CONFIDENCE_ASSIGNED):
            found.append(
                Candidate(
                    _base_of(match.group(1)),
                    value,
                    match.start(3),
                    match.end(3),
                    CONFIDENCE_ASSIGNED,
                )
            )
    for match in _BEARER_RE.finditer(text):
        value = match.group(3)
        if shape_ok(value, CONFIDENCE_ASSIGNED):
            found.append(
                Candidate("bearer", value, match.start(3), match.end(3), CONFIDENCE_ASSIGNED)
            )
    if profile != PROFILE_CODE:
        found.extend(_entropy_candidates(text))
    return _prune(found)


def redact(
    text: str,
    *,
    profile: str = PROFILE_OUTPUT,
    name_for: NameFor,
    sink: Sink | None = None,
) -> tuple[str, list[Applied]]:
    """把候选换成 `{{secret:name}}`，返回新文本与实际替换清单。

    替换按跨度从右到左进行，因此**不会**碰到同一标识符在别处的其它出现：
    旧实现用 `str.replace` 全量替换，命中一次就把 `def estimate_prefix_tokens`
    的调用点、定义点一起改掉，源码直接不可编译。

    `name_for(base, value)` 由调用方决定命名策略（输入侧按值编号、输出侧复用
    保险箱已有名字）；`sink` 可选，用于登记明文。
    """
    protected, slots = _protect_placeholders(text)
    applied: dict[tuple[str, str], Applied] = {}
    out = protected
    candidates = sorted(
        find_candidates(protected, profile=profile), key=lambda c: c.start, reverse=True
    )
    for cand in candidates:
        key = (cand.base, cand.value)
        item = applied.get(key)
        if item is None:
            name = name_for(cand.base, cand.value)
            if sink is not None:
                sink(name, cand.value)
            item = Applied(cand.base, name, cand.value, cand.confidence)
            applied[key] = item
        out = f"{out[: cand.start]}{{{{secret:{item.name}}}}}{out[cand.end :]}"
        item.count += 1
    return _restore_placeholders(out, slots), list(applied.values())


def identifiers_free(text: str, *, profile: str = PROFILE_CODE) -> bool:
    """自检用：这段文本里没有任何候选凭据（全库扫描门禁的判定入口）。"""
    return not find_candidates(text, profile=profile)


__all__ = [
    "Applied",
    "CONFIDENCE_ASSIGNED",
    "CONFIDENCE_ENTROPY",
    "CONFIDENCE_KNOWN",
    "Candidate",
    "PLACEHOLDER_PATTERN",
    "PROFILE_CODE",
    "PROFILE_INPUT",
    "PROFILE_OUTPUT",
    "find_candidates",
    "identifiers_free",
    "redact",
    "shape_ok",
    "shannon_entropy",
]
