"""长期写入的抽取式摘要与条目归一化（SPEC-0016 §8）。

为什么长期层不能存回合原文：原文含大量寒暄与过程话术，词面重合度被稀释，
检索反而更差；同时「同义改写」几乎永远不会命中精确去重，条目只增不减。
这里做两件事：

1. **抽句**：按信号词与结论词挑句子，压到 240 字以内，不调模型（离线可测）；
2. **归一化 + 相似判定**：给写入侧的去重与强化提供口径（3-gram Jaccard）。

`normalize` 与 `similar` 不放进仓储：它们判断的是「两段话是不是同一件事」，
属于记忆语义，不属于 SQL 存取。
"""

from __future__ import annotations

import re
from typing import Final

from devops_agent.memory.candidate import _SIGNAL

#: 句子边界：中文句号/问号/叹号与换行；英文句点后跟空格也算
_SENTENCE = re.compile(r"[^。！？\n!?]+[。！？?]?")
#: 约定词：出现即说明「这条要长期记住」，优先级高于普通事实
_CONVENTION: Final = ("默认", "以后", "记住", "约定", "必须", "不要", "一律", "不许")
#: 结论词：说明这句是「想清楚了什么」，比过程描述更值得留
_CONCLUSION: Final = ("因此", "所以", "结论", "根因", "修复方案", "原因是", "改成")
#: 句首代词：抽出来的句子常常以「那/这/它」开头，离开上下文就读不懂
_LEAD_PRONOUN = re.compile(r"^(那么?|这个?|那个?|它|上述|该|其实|另外)[，,、\s]*")
_STEP = re.compile(r"(^|\n)\s*(\d+[.、)]|[一二三四五六七八九十]+[、.])")
#: 「命令 token」：`--flag`、带路径分隔符的路径、`./x`、以及常见的命令行工具。
#: 不能把所有 ASCII 词都算进来 —— `200` / `QPS` 会让任何一句带数字的话都变成
#: `procedure`（`kind` 参与检索加权，误分类等于给噪音加权）。
_CMD = re.compile(
    r"(?:^|\s)(?:--?[A-Za-z][\w-]*"
    r"|[A-Za-z][\w.-]*(?:/[A-Za-z0-9_.-]+)+"
    r"|\./[\w./-]+"
    r"|(?:git|pytest|npm|pnpm|yarn|curl|docker|kubectl|python|ruff|mypy|make)\b)",
)
_PUNCT = re.compile(r"[\s，,。.！!？?；;：:、\"'“”‘’（）()\[\]【】<>《》…—\-]+")


def split_sentences(text: str) -> list[str]:
    """按中文句读与换行切句，去掉空白句。"""
    return [chunk.strip() for chunk in _SENTENCE.findall(text) if chunk.strip()]


def _clean(sentence: str) -> str:
    return _LEAD_PRONOUN.sub("", sentence).strip()


def _priority(sentence: str) -> int:
    """越小越优先：路径/命令 > 约定词 > 结论词 > 其余。"""
    if _SIGNAL.search(sentence):
        return 0
    if any(word in sentence for word in _CONVENTION):
        return 1
    if any(word in sentence for word in _CONCLUSION):
        return 2
    return 3


def _pick(sentences: list[str], max_chars: int) -> list[int]:
    """按优先级挑句子直到字数上限；返回按原文顺序排好的下标。

    稳定排序：优先级相同的句子保持原文顺序，摘要读起来才像话。
    至少留一句（哪怕它自己就超长）—— 一条不选的摘要是空记忆。
    """
    ranked = sorted(enumerate(sentences), key=lambda pair: (_priority(pair[1]), pair[0]))
    picked: list[int] = []
    used = 0
    for index, sentence in ranked:
        cost = len(sentence) + 1
        if used + cost > max_chars and picked:
            continue
        picked.append(index)
        used += cost
        if used >= max_chars:
            break
    return sorted(picked)


def summarize(text: str, *, max_chars: int = 240) -> tuple[str, bool]:
    """抽句压到 `max_chars` 以内；返回 (正文, 是否降级)。

    降级 = 抽句没起作用：一条都没抽出来（退回原文前缀），或抽出来的正文本身
    还得硬截断。静默写一条「看起来像摘要」的原文前缀比降级更糟 —— 之后每一轮
    都会被召回，还没人知道它没被整理过。
    """
    sentences = [_clean(item) for item in split_sentences(text)]
    sentences = [item for item in sentences if item]
    if not sentences:
        fallback = text.strip()[:max_chars]
        return fallback, bool(fallback)

    body = "\n".join(sentences[index] for index in _pick(sentences, max_chars))
    if not body.strip():
        return text.strip()[:max_chars], True
    return body[:max_chars], len(body) > max_chars


#: 条目类型：参与检索加权（决策 ×1.2），也决定注入时的前缀
KINDS: Final = ("fact", "procedure", "decision")


def kind_of(text: str) -> str:
    """`decision`（约定/禁止）> `procedure`（≥2 个命令 token 或编号步骤）> `fact`。"""
    if any(word in text for word in ("默认", "以后", "一律", "不许", "不要")):
        return "decision"
    commands = {item.lower() for item in _CMD.findall(text)}
    if len(commands) >= 2 or _STEP.search(text):
        return "procedure"
    return "fact"


def normalize(text: str) -> str:
    """去标点空白并小写；中文保留字序（不做分词，避免同义不同词的问题复杂化）。"""
    return _PUNCT.sub("", text.lower())


def ngrams(text: str, size: int = 3) -> set[str]:
    """字符 n-gram；短于 n 时退化为整串，保证非空。"""
    body = normalize(text)
    if len(body) <= size:
        return {body} if body else set()
    return {body[index : index + size] for index in range(len(body) - size + 1)}


def similar(left: str, right: str, *, threshold: float = 0.8) -> bool:
    """3-gram Jaccard ≥ threshold 视为同一条（同义改写的典型相似度 0.8~0.9）。"""
    lhs, rhs = ngrams(left), ngrams(right)
    if not lhs or not rhs:
        return False
    return len(lhs & rhs) / len(lhs | rhs) >= threshold
