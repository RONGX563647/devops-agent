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

from devops_agent.memory_policy import LongTermPolicy, MemoryPolicy
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


def resolve_scope_id(policy: MemoryPolicy, session_id: str, scope: str | None = None) -> str:
    """解析长期记忆的 scope_id：用户级优先用 user_id，否则回落到会话键。

    `scope` 显式传入时以它为准（管理接口支持按查询参数切换），
    没传就按策略默认。两条路径共用同一规则，避免读写落在不同分区。
    """
    effective = scope or policy.long_term.scope
    if effective == "user" and policy.user_id:
        return policy.user_id
    return f"session:{session_id}"


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
            # 候选池至少覆盖全部保留条目：只取最近几十条会让高价值旧结论永远召不回
            rows = repo.list_long_term(
                conn,
                scope=self._policy.scope,
                scope_id=self._scope_id,
                limit=max(self._policy.max_entries, top_k * 20),
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


def build_long_term(
    policy: LongTermPolicy, scope_id: str, db_path: Path | None
) -> tuple[LongTermBackend, str]:
    """按配置选后端；外部后端未配置时降级为 off 并返回原因。

    当前只有 local 可在无额外依赖时工作。mem0 / agentic 需要外部客户端或
    工作目录约定，未接入前显式降级，而不是给出「有记忆」的假象。
    """
    if not policy.enabled or policy.backend == "off":
        return OffLongTerm("长期记忆未启用"), "off"
    if policy.backend in ("mem0", "agentic"):
        return OffLongTerm(f"后端 {policy.backend} 未配置，已降级为关闭"), "off"
    return LocalLongTerm(policy, scope_id, db_path), "local"
