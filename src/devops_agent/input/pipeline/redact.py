"""密钥脱敏：识别凭据、生成占位符、把明文换成 {{secret:name}}。

规则本身在 `devops_agent.redaction.rules`：输入侧、输出侧与读源码三个档案共用
同一份判定与同一套闸门，本模块只做输入侧特有的事——按值编号命名、指纹打码、
阶段事件。改判据只改规则模块，不要在这里加第二条正则。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Final

from devops_agent.redaction import rules

from .model import Reference, Stage, _elapsed_ms, _stage

# 占位符语法与 secrets/vault.py 的 PLACEHOLDER_PATTERN 必须逐字一致，
# 两边由 tests/unit/test_secrets.py 里的一条对账用例钉住，改一处就会红。
_PLACEHOLDER_RE: Final = rules.PLACEHOLDER_PATTERN

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


def _redact_text(text: str, namer: _SecretNamer, sink: SecretSink | None) -> tuple[str, list[str]]:
    """把命中的密钥替换成 {{secret:name}}，返回替换记录。

    同一条 (规则, 值) 只记一行（旧实现每个出现位置记一行，`items` 计数会虚高）；
    占位符保护与跨度替换由规则模块统一负责。
    """

    def _name_for(base: str, value: str) -> str:
        name = namer.name_for(base, value)
        if sink is not None:
            sink(name, value)
        return name

    clean, applied = rules.redact(text, profile=rules.PROFILE_INPUT, name_for=_name_for)
    notes = [
        f"{item.name} · {_fingerprint(item.value)} → {{{{secret:{item.name}}}}}" for item in applied
    ]
    return clean, notes


def redact_preview(text: str) -> str:
    """对外门面：只脱敏、不落保险箱的预览版（给「落库前」用）。

    网关需要在解析管线**之前**先脱一遍（观测库的 input 列要求脱敏，而 recorder
    必须在管线之前 start），两边必须是同一套规则且对已有占位符幂等 —— 由本函数保证，
    调用方不再 import `_redact_text` / `_SecretNamer` 这些内部名字。
    """
    clean, _ = _redact_text(text, _SecretNamer(), None)
    return clean


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
