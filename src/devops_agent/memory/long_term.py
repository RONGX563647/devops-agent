"""长期记忆后端 —— local 兜底与外部后端降级。

不手搓向量库：local 只是会话级 SQLite + 词面重合度评分，用来在
mem0 / agentic 未配置时仍能跑通「检索 → 注入 → 写入」闭环并留下轨迹。
外部后端可用时由 build_long_term 切换；不可用时明确报 reason，不静默假装有记忆。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devops_agent.memory_policy import LongTermPolicy
from devops_agent.observability import repository as repo

_WORDS = re.compile(r"[a-zA-Z0-9_]{2,}")


@dataclass(frozen=True, slots=True)
class Hit:
    """一条长期命中；score 只用于排序与 min_score 过滤。"""

    id: int
    text: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "score": self.score}


def _tokens(text: str) -> set[str]:
    """ASCII 词 + 中文二元组：不依赖分词器，足够做词面召回。"""
    words = {word.lower() for word in _WORDS.findall(text)}
    cjk = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    words.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return {word for word in words if word}


def score(query: str, text: str) -> float:
    """查询与条目的重合度（0~1）：交集 / 查询词数的平方根。"""
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & _tokens(text))
    return min(1.0, overlap / math.sqrt(len(query_tokens)))


class LocalLongTerm:
    """会话级 SQLite 长期记忆（兜底实现）。"""

    def __init__(self, policy: LongTermPolicy, scope_id: str, db_path: Path | None) -> None:
        self._policy = policy
        self._scope_id = scope_id
        self._db_path = db_path

    @property
    def name(self) -> str:
        return "local"

    async def search(self, query: str, top_k: int, min_score: float) -> list[Hit]:
        """词面召回 + 阈值过滤，按分数取前 top_k。"""
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            rows = repo.list_long_term(
                conn,
                scope=self._policy.scope,
                scope_id=self._scope_id,
                limit=max(top_k * 20, 50),
            )
        hits = [
            Hit(id=int(row["id"]), text=str(row["text"]), score=score(query, str(row["text"])))
            for row in rows
        ]
        kept = [hit for hit in hits if hit.score >= min_score]
        kept.sort(key=lambda hit: hit.score, reverse=True)
        return kept[:top_k]

    async def write(self, text: str, kind: str = "fact") -> dict[str, Any]:
        """按 (scope, scope_id, text) 去重写入；返回 stored/deduped。"""
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            return repo.save_long_term(
                conn,
                scope=self._policy.scope,
                scope_id=self._scope_id,
                kind=kind,
                text=text,
                ttl_days=self._policy.ttl_days,
                max_entries=self._policy.max_entries,
            )


class OffLongTerm:
    """关闭状态：检索恒空、写入 no-op，但事件照发（可解释「为什么没有记忆」）。"""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    @property
    def name(self) -> str:
        return "off"

    @property
    def reason(self) -> str:
        return self._reason

    async def search(self, query: str, top_k: int, min_score: float) -> list[Hit]:
        return []

    async def write(self, text: str, kind: str = "fact") -> dict[str, Any]:
        return {"stored": False, "deduped": False, "id": None}


LongTermBackend = LocalLongTerm | OffLongTerm

#: 真正实现了的后端。枚举里可以请求别的，但 effective 只会落在这里。
_IMPLEMENTED = ("local",)


@dataclass(frozen=True, slots=True)
class BackendChoice:
    """一次后端解析的结果：请求了什么、实际用了什么、为什么不是请求的那个。

    三者必须同时存在 —— 只报 effective 会让「配了 mem0」看起来像生效了，
    只报 requested 又会让状态页把降级说成没配置（SPEC-0013 §18.2 M1）。
    """

    backend: LongTermBackend
    requested: str
    effective: str
    reason: str = ""


def build_long_term(policy: LongTermPolicy, scope_id: str, db_path: Path | None) -> BackendChoice:
    """按配置选后端；请求未接入的后端时降级为 local 并给出原因。

    当前只有 local 可在无额外依赖时工作。`auto` 解析到 local；
    `mem0` / `agentic` 未接入，降级为 local 而不是 off —— 降级到 off 等于
    「因为配置了一个更强的后端，反而失去了兜底记忆」。
    """
    requested = str(policy.backend)
    if not policy.enabled:
        return BackendChoice(OffLongTerm("长期记忆未启用"), requested, "off", "长期记忆未启用")
    if requested == "off":
        return BackendChoice(OffLongTerm("长期记忆已关闭"), requested, "off", "长期记忆已关闭")
    local = LocalLongTerm(policy, scope_id, db_path)
    if requested in _IMPLEMENTED or requested == "auto":
        return BackendChoice(local, requested, "local")
    return BackendChoice(local, requested, "local", f"{requested} 未接入，已降级为 local")
