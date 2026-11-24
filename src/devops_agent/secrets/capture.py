"""输出侧敏感信息捕获 —— 工具自己读到的明文也不许裸奔进上下文。

输入侧由 `input/pipeline.py` 的 `_redact` 挡住「用户贴进来的 key」，但工具
执行时自己从磁盘读到的明文（`Bash: cat config.yaml`、`printenv`、脚本回显）
走的是另一条路：它不在 resolved 里，中间件原来的 mask 对它无能为力，于是
原样进模型上下文、spans 表、前端 trace。

这一层补上出口兜底：识别输出里的明文凭据，**登记进保险箱**（而不是静默
丢弃——用户可能还想用/查看它），正文换成 `{{secret:name}}` 占位符。这样：
- 模型看到的是占位符，明文不进下一轮上下文；
- 保险箱里多一条 `source=output` 的记录，人可以在密钥页查看/复用/删除；
- 同值复用同一名字，不重复入库。

规则与输入侧一致（6 类前缀 + 赋值式），有意各自定义而不是跨层 import
`input.pipeline`：`secrets` 与 `input` 是平级模块，不该产生反向依赖。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from devops_agent.secrets.vault import placeholder

# 与 input/pipeline.py 的 _SECRET_PATTERNS 同源（见模块 docstring 关于不 import 的说明）
_KNOWN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
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
# 高熵兜底：同行含凭据语义词 + 长且乱的串。宁可标 `?` 让人复核，也不静默放行。
_ASSIGNED_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|secret|token|password|passwd)"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-.]{20,})"
)
_SECRET_HINT_RE = re.compile(r"(?i)key|secret|token|passwd|password|bearer")
_ENTROPY_MIN = 3.5
_VALUE_MIN = 20
_FP_LEN = 8


@dataclass(frozen=True, slots=True)
class Captured:
    """一条被捕获的敏感串：名字与原文，供登记入库。"""

    name: str
    value: str
    base: str


def shannon_entropy(value: str) -> float:
    """字符分布的 Shannon 熵：纯数字/重复串低，随机 token 高。"""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _name_for(base: str, value: str) -> str:
    """确定性命名：同值同名（跨调用稳定），短指纹避免占位符撞名。"""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FP_LEN]
    return f"out_{base}_{digest}"


def find_secrets(
    text: str, *, existing: Callable[[str], str | None] | None = None
) -> list[Captured]:
    """扫描一段文本里需要捕获的明文凭据。

    `existing(value)` 用于复用已入库的同值名字（由调用方传 vault 的反查函数），
    避免重复登记；不传则按值生成确定性名字。
    """
    found: list[Captured] = []
    seen: set[str] = set()

    def add(base: str, value: str) -> None:
        if value in seen:
            return
        seen.add(value)
        known = existing(value) if existing is not None else None
        found.append(Captured(name=known or _name_for(base, value), value=value, base=base))

    for pattern_name, pattern in _KNOWN_PATTERNS:
        for match in pattern.finditer(text):
            add(pattern_name, match.group(0))

    for match in _ASSIGNED_RE.finditer(text):
        value = match.group(2)
        if len(value) >= _VALUE_MIN and shannon_entropy(value) >= _ENTROPY_MIN:
            add("assigned", value)

    # 高熵兜底：逐个「候选词」判定，避免整段文本被当成一个 token。
    for match in re.finditer(r"[A-Za-z0-9_\-.+/=]{20,}", text):
        value = match.group(0)
        if shannon_entropy(value) < _ENTROPY_MIN:
            continue
        line = text[max(0, match.start() - 60) : match.end() + 20]
        if _SECRET_HINT_RE.search(line):
            add("entropy", value)

    return found


def capture_output(
    text: str,
    *,
    register: Callable[[Captured], str],
) -> tuple[str, list[Captured]]:
    """把输出里的明文凭据登记入库并换成占位符，返回 (新文本, 捕获清单)。

    `register(item)` 由调用方实现「登记进 vault」并返回最终名字；这样本模块
    不依赖 vault 实例，纯逻辑可单测，也便于在保箱不可用时降级（返回原名）。
    """
    items = find_secrets(text)
    if not items:
        return text, []
    out = text
    captured: list[Captured] = []
    for item in items:
        name = register(item)
        out = out.replace(item.value, placeholder(name))
        captured.append(Captured(name=name, value=item.value, base=item.base))
    return out, captured
