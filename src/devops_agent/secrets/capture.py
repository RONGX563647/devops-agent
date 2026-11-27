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

判定与输入侧**同一份**（`devops_agent.redaction.rules`）。早期这里自己维护
一份正则表，和输入侧已经漂移出三处差异；更严重的是那段高熵兜底把 Python
标识符当凭据、又用 `str.replace` 全量替换，读源码的工具输出会被改成不可编译
的文本。现在按档案区分：读源码（`profile="code"`）只保留已知前缀与赋值式。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from devops_agent.redaction import rules

# 指纹长度：占位符名的短指纹，避免同值多次捕获时撞名
_FP_LEN = 8


@dataclass(frozen=True, slots=True)
class Captured:
    """一条被捕获的敏感串：名字与原文，供登记入库。"""

    name: str
    value: str
    base: str
    confidence: str = rules.CONFIDENCE_ASSIGNED


def shannon_entropy(value: str) -> float:
    """字符分布的 Shannon 熵：纯数字/重复串低，随机 token 高（规则模块实现）。"""
    return rules.shannon_entropy(value)


def _name_for(base: str, value: str) -> str:
    """确定性命名：同值同名（跨调用稳定），短指纹避免占位符撞名。"""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FP_LEN]
    return f"out_{base}_{digest}"


def find_secrets(
    text: str,
    *,
    existing: Callable[[str], str | None] | None = None,
    profile: str = rules.PROFILE_OUTPUT,
) -> list[Captured]:
    """扫描一段文本里需要捕获的明文凭据（只读，不改文本）。

    `existing(value)` 用于复用已入库的同值名字（由调用方传 vault 的反查函数），
    避免重复登记；不传则按值生成确定性名字。`profile="code"` 关闭熵兜底。
    """
    found: list[Captured] = []
    seen: set[str] = set()
    for candidate in rules.find_candidates(text, profile=profile):
        if candidate.value in seen:
            continue
        seen.add(candidate.value)
        known = existing(candidate.value) if existing is not None else None
        found.append(
            Captured(
                name=known or _name_for(candidate.base, candidate.value),
                value=candidate.value,
                base=candidate.base,
                confidence=candidate.confidence,
            )
        )
    return found


def capture_output(
    text: str,
    *,
    register: Callable[[Captured], str],
    profile: str = rules.PROFILE_OUTPUT,
) -> tuple[str, list[Captured]]:
    """把输出里的明文凭据登记入库并换成占位符，返回 (新文本, 捕获清单)。

    `register(item)` 由调用方实现「登记进 vault」并返回最终名字；这样本模块
    不依赖 vault 实例，纯逻辑可单测，也便于在保箱不可用时降级（返回原名）。
    替换按命中跨度进行，不再全量 `str.replace`——那是上一个版本的破坏源。
    """
    names: dict[str, str] = {}

    def _register_once(base: str, value: str) -> str:
        if value not in names:
            names[value] = register(Captured(_name_for(base, value), value, base))
        return names[value]

    out, applied = rules.redact(text, profile=profile, name_for=_register_once)
    captured = [
        Captured(name=item.name, value=item.value, base=item.base, confidence=item.confidence)
        for item in applied
    ]
    return out, captured
