"""长期记忆后端 —— local 兜底与外部后端降级。

不手搓向量库：local 只是会话级 SQLite + 词面召回，用来在 mem0 / agentic
未配置时仍能跑通「检索 → 注入 → 写入」闭环并留下轨迹。
外部后端可用时由 build_long_term 切换；不可用时明确报 reason，不静默假装有记忆。

SPEC-0016 之后 local 有两处实现替换：

- 检索默认 BM25（`retrieval: bm25`），`overlap` 保留一个版本用于 A/B 与回滚；
- 写入先归一化去重（3-gram ≥0.8 视为同一条）再落库，条目数不再只增不减。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devops_agent.memory.bm25 import BM25Index, Doc, Scored
from devops_agent.memory.summarize import normalize, similar
from devops_agent.memory_policy import LongTermPolicy
from devops_agent.observability import repository as repo

_WORDS = re.compile(r"[a-zA-Z0-9_]{2,}")


@dataclass(frozen=True, slots=True)
class Hit:
    """一条长期命中；`score` 归一化到 0~1，`raw` 是原始分（仅排障）。"""

    id: int
    text: str
    score: float
    raw: float = 0.0
    kind: str = "fact"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "score": self.score, "kind": self.kind}


def _tokens(text: str) -> set[str]:
    """ASCII 词 + 中文二元组：`overlap` 回滚路径用的旧口径，保持原样。"""
    words = {word.lower() for word in _WORDS.findall(text)}
    cjk = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    words.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return {word for word in words if word}


def score(query: str, text: str) -> float:
    """旧的重合度打分（`retrieval: overlap` 时使用）：交集 / 查询词数的平方根。

    这个口径**饱和得快**：短查询命中两个 2-gram 就封顶 1.00，`min_score` 的
    区分度因此很低（SPEC-0019 §7.3）。改成线性归一看起来更合理，但评测阈值
    `EVAL_MIN_SCORE=0.7` 是照这个口径标定的（等价于「≥2 个 2-gram 命中」），
    换口径必须同轮重标定整条评测基线——留给独立一轮，不混进裁剪修复。
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    overlap = len(query_tokens & _tokens(text))
    return min(1.0, overlap / math.sqrt(len(query_tokens)))


class LocalLongTerm:
    """会话级 SQLite 长期记忆（兜底实现）。"""

    def __init__(
        self,
        policy: LongTermPolicy,
        scope_id: str,
        db_path: Path | None,
        scope: str | None = None,
    ) -> None:
        self._policy = policy
        self._scope_id = scope_id
        # 绑定工作区的会话由装配层显式传 `scope="workspace"`：检索边界由
        # scope + scope_id 一起决定，策略里的默认值只是未绑定时的回落
        self._scope = scope or policy.scope
        self._db_path = db_path
        #: BM25 索引 + 失效键 `(条数, 最大 id)`；写入路径还会主动置空，
        #: 避免「合并改了正文但签名没变」导致的脏索引
        self._index: BM25Index | None = None
        self._index_key: tuple[int, int] | None = None

    @property
    def name(self) -> str:
        return "local"

    def _rows(self, conn: Any) -> list[dict[str, Any]]:
        return repo.list_long_term(
            conn,
            scope=self._scope,
            scope_id=self._scope_id,
            all_entries=True,
            max_entries=self._policy.max_entries,
        )

    def _bm25(self, rows: list[dict[str, Any]]) -> BM25Index:
        key = (len(rows), max((int(row["id"]) for row in rows), default=0))
        if self._index is not None and self._index_key == key:
            return self._index
        index = BM25Index()
        index.build(
            [Doc(id=int(row["id"]), text=str(row["text"]), kind=str(row["kind"])) for row in rows]
        )
        self._index, self._index_key = index, key
        return index

    @staticmethod
    def _hit(item: Scored) -> Hit:
        return Hit(
            id=item.doc.id,
            text=item.doc.text,
            score=round(item.score, 4),
            raw=round(item.raw, 4),
            kind=item.doc.kind,
        )

    def _overlap_search(
        self, query: str, rows: list[dict[str, Any]], top_k: int, min_score: float
    ) -> list[Hit]:
        hits = [
            Hit(id=int(row["id"]), text=str(row["text"]), score=score(query, str(row["text"])))
            for row in rows
        ]
        kept = [hit for hit in hits if hit.score >= min_score]
        kept.sort(key=lambda hit: hit.score, reverse=True)
        return kept[:top_k]

    async def search(self, query: str, top_k: int, min_score: float) -> list[Hit]:
        """全量取数后打分；命中条目回写 `hits/last_hit_at`（淘汰顺序的依据）。"""
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            rows = self._rows(conn)
            if not rows:
                return []
            if self._policy.retrieval == "overlap":
                hits = self._overlap_search(query, rows, top_k, min_score)
            else:
                hits = [
                    self._hit(item) for item in self._bm25(rows).search(query, top_k, min_score)
                ]
            repo.touch_long_term(conn, [hit.id for hit in hits])
        return hits

    async def write(self, text: str, kind: str = "fact") -> dict[str, Any]:
        """归一化去重写入：相似条目合并（累加 hits）而不是新增一行。"""
        normalized = normalize(text)
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            match = next((row for row in self._rows(conn) if similar(text, str(row["text"]))), None)
            if match is not None:
                keep = text if len(text) >= len(str(match["text"])) else str(match["text"])
                result = repo.merge_long_term(
                    conn,
                    int(match["id"]),
                    text=keep,
                    norm_text=normalize(keep),
                    kind=kind,
                )
            else:
                result = repo.save_long_term(
                    conn,
                    scope=self._scope,
                    scope_id=self._scope_id,
                    kind=kind,
                    text=text,
                    ttl_days=self._policy.ttl_days,
                    max_entries=self._policy.max_entries,
                    norm_text=normalized,
                )
            # 正文可能在被合并时变长：签名相同也要让索引失效
            self._index, self._index_key = None, None
            return result


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


def build_long_term(
    policy: LongTermPolicy,
    scope_id: str,
    db_path: Path | None,
    scope: str | None = None,
) -> BackendChoice:
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
    local = LocalLongTerm(policy, scope_id, db_path, scope)
    if requested in _IMPLEMENTED or requested == "auto":
        return BackendChoice(local, requested, "local")
    return BackendChoice(local, requested, "local", f"{requested} 未接入，已降级为 local")
