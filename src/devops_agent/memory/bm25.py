"""BM25 召回 —— 纯 Python 内存索引，不依赖 FTS5（SPEC-0016 §7）。

为什么不用 SQLite FTS5：中文要预分词成空格隔开的二元组才能配合 `unicode61`，
等于把分词器搬到写入侧；而 `max_entries=200` 的量级下重建索引 <2ms，
持久化换来的收益抵不上跨平台差异。

打分用标准 BM25（k1=1.2, b=0.75），再按「同一 query 对理想文档的得分」归一化：
`norm = min(1, score / ideal)`。归一化不是为了好看 —— 旧的
`overlap / sqrt(len(query_tokens))` 与文档长度无关，阈值实际是「短条目优先」
的隐式偏好；归一化后 `min_score` 才真的是「像不像」而不是「短不短」。

分词器针对 DevOps 语料补了三类信号：路径切分（`app.py:120`、`a/b`）、
子词切分（`snake_case`、`readFile`）、数字串（版本号/端口/issue 号）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

#: 路径与命令行分隔符：切开后每一段单独成词（`.` 也切，`v1.5.2` → v1/5/2）
_SPLIT = re.compile(r"[\\/.:|;,&=(){}\[\]'\"<>]+")
_RUN = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|\d+|[a-z]+")

#: BM25 参数：k1 控制词频饱和，b 控制长度归一化强度（常用默认值）
K1: Final = 1.2
B: Final = 0.75
#: 条目类型权重：决策比事实更值得被读到（SPEC-0016 §8.3）
KIND_WEIGHT: Final = {"decision": 1.2, "procedure": 1.1, "fact": 1.0}


@dataclass(frozen=True, slots=True)
class Doc:
    """索引里的一条文档；`kind` 参与加权。"""

    id: int
    text: str
    kind: str = "fact"


@dataclass(frozen=True, slots=True)
class Scored:
    """一条命中：`score` 是归一化后的 0~1，`raw` 是原始 BM25 分（排障用）。"""

    doc: Doc
    score: float
    raw: float


def _subwords(run: str) -> list[str]:
    """`snake_case` 按 `_` 切、`readFile` 按大小写切；单词本身不重复返回。"""
    parts = [item for item in run.split("_") if item]
    if len(parts) > 1:
        return [item.lower() for item in parts]
    camel = _CAMEL.findall(run)
    return [item.lower() for item in camel] if len(camel) > 1 else []


def tokenize(text: str) -> list[str]:
    """保留词频（list 而非 set）：BM25 的 tf 就是重复次数。"""
    tokens: list[str] = []
    for chunk in _SPLIT.split(text):
        for run in _RUN.findall(chunk):
            tokens.append(run.lower())
            tokens.extend(_subwords(run))
        for cjk_run in _CJK.findall(chunk):
            if len(cjk_run) == 1:
                tokens.append(cjk_run)
            else:
                tokens.extend(cjk_run[index : index + 2] for index in range(len(cjk_run) - 1))
    return tokens


class BM25Index:
    """一个 (scope, scope_id) 的内存索引；重建成本线性于语料，量级很小。"""

    def __init__(self, k1: float = K1, b: float = B) -> None:
        self._k1 = k1
        self._b = b
        self._docs: list[Doc] = []
        self._tf: dict[str, dict[int, int]] = {}
        self._lengths: list[int] = []
        self._avgdl = 0.0

    def __len__(self) -> int:
        return len(self._docs)

    def build(self, docs: list[Doc]) -> None:
        """全量重建；`tf` 用下标而不是 id，省一次字典查找。"""
        self._docs = list(docs)
        self._tf = {}
        self._lengths = []
        for index, doc in enumerate(self._docs):
            tokens = tokenize(doc.text)
            self._lengths.append(len(tokens) or 1)
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, count in counts.items():
                self._tf.setdefault(token, {})[index] = count
        self._avgdl = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

    def _idf(self, term: str) -> float:
        total = len(self._docs)
        df = len(self._tf.get(term, {}))
        return math.log(1 + (total - df + 0.5) / (df + 0.5))

    def _score(self, index: int, terms: list[str]) -> float:
        """单文档得分；分母里的 `dl/avgdl` 是长度归一化。"""
        dl = self._lengths[index]
        denom_base = self._k1 * (1 - self._b + self._b * dl / (self._avgdl or 1.0))
        total = 0.0
        for term in terms:
            tf = self._tf.get(term, {}).get(index)
            if not tf:
                continue
            total += self._idf(term) * (tf * (self._k1 + 1)) / (tf + denom_base)
        return total

    def _ideal(self, terms: list[str]) -> float:
        """理想文档（每个词出现 1 次、长度等于均值）的得分，作为归一化分母。

        `dl/avgdl = 1` 时分母退化为 `1 + k1`，与分子约掉，所以理想分就是
        「出现在语料里的查询词的 idf 之和」—— 这样归一化与文档长度彻底解耦。
        """
        return sum(self._idf(term) for term in terms if term in self._tf)

    def search(self, query: str, top_k: int, min_score: float) -> list[Scored]:
        """按归一化得分取前 `top_k`；低于 `min_score` 的丢弃。"""
        if not self._docs:
            return []
        terms = sorted(set(tokenize(query)))
        if not terms:
            return []
        ideal = self._ideal(terms)
        if ideal <= 0:
            return []
        scored: list[Scored] = []
        for index, doc in enumerate(self._docs):
            raw = self._score(index, terms) * KIND_WEIGHT.get(doc.kind, 1.0)
            if raw <= 0:
                continue
            score = min(1.0, raw / ideal)
            if score >= min_score:
                scored.append(Scored(doc=doc, score=score, raw=raw))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[: max(0, top_k)]
