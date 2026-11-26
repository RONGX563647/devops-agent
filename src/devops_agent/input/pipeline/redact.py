"""密钥脱敏：识别凭据、生成占位符、把明文换成 {{secret:name}}。

- 命中后整段替换成 {{secret:name}}，指纹只留前 4 位与长度；
- 占位符语法与 secrets/vault.py 的 PLACEHOLDER_PATTERN 必须逐字一致，
  两边由 tests/unit/test_secrets.py 里的一条对账用例钉住，改一处就会红。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from typing import Final

from .model import Reference, Stage, _elapsed_ms, _stage

# 占位符语法：与 secrets/vault.py 的 PLACEHOLDER_PATTERN 必须逐字一致，
# 两边由 tests/unit/test_secrets.py 里的一条对账用例钉住，改一处就会红。
_PLACEHOLDER_RE: Final = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
# 脱敏中途的临时槽位：用私用区字符，任何密钥正则都不会命中它
_SLOT_START: Final = "\ue000"
_SLOT_END: Final = "\ue001"
_SLOT_RE: Final = re.compile(_SLOT_START + r"(\d+)" + _SLOT_END)

# 密钥识别规则：命中后整段替换成 {{secret:name}}，指纹只留前 4 位与长度
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
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
# 前缀式 env 名（API_TOKEN / DB_PASSWORD）此前漏检：`\b` 在 `_` 前不成立，
# 旧规则只认裸 token=；这里把 `FOO_` 前缀并入 group(1)，仍只替换等号后的值。
_ASSIGNED_SECRET_RE: Final = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key|secret|token|password|passwd))"
    r"(\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9_\-.]{8,})"
)
# Bearer 不是赋值语法，单独保留前缀，避免把 "Bearer " 一起吞进占位符
_BEARER_RE: Final = re.compile(r"(?i)\b(Bearer)(\s+)([A-Za-z0-9._\-]{10,})")

# 凭据落库回调：网关传进来（接保险箱），管线本身不认识存储
SecretSink = Callable[[str, str], None]


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


# 与 secrets.vault 的 _HINT_KEEP_CHARS / _HINT_MIN_LEN 保持一致：短值一个字符都不露。
# 不跨层 import vault——那会把 cryptography 拖进纯 stdlib 的输入管线（离线门禁要能跑）；
# 两边由 tests/unit/test_secrets.py 的一致性用例钉住，改一处就会红。
_HINT_KEEP_CHARS = 4
_HINT_MIN_LEN = 12


def _fingerprint(secret: str) -> str:
    """给人看的识别串：长值露前 4 位与长度，短值只给长度。

    早期版本无条件 `secret[:4]`，短 key 会把整串露进 input_stages.detail——
    那里整段落库并推给前端，与保险箱的严格阈值不一致，属于同源规则分叉。
    """
    if len(secret) <= _HINT_MIN_LEN:
        return f"（{len(secret)} 位）"
    return f"{secret[:_HINT_KEEP_CHARS]}…（{len(secret)} 位）"


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
    """替换 `api_key=...` / `Bearer ...` 这类显式凭据（保留键名，只换值）。"""

    def _replace(match: re.Match[str]) -> str:
        base = match.group(1).lower().replace("-", "_")
        value = match.group(3)
        name = namer.name_for(base, value)
        if sink is not None:
            sink(name, value)
        notes.append(f"{name} · {_fingerprint(value)} → {{{{secret:{name}}}}}")
        return f"{match.group(1)}{match.group(2)}{{{{secret:{name}}}}}"

    return _BEARER_RE.sub(_replace, _ASSIGNED_SECRET_RE.sub(_replace, text))


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
