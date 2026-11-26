"""中期（会话情景）记忆卡：版本化写入、active 读取与删除。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devops_agent.memory_policy import EpisodicPolicy
from devops_agent.observability import repository as repo


class EpisodicMemory:
    """会话中期卡片的仓储门面；版本切换的原子性由 repository 保证。"""

    def __init__(self, policy: EpisodicPolicy, db_path: Path | None) -> None:
        self._policy = policy
        self._db_path = db_path

    def active(self, session_id: str) -> dict[str, Any] | None:
        if not self._policy.enabled:
            return None
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            return repo.active_memory(conn, session_id)

    def save(
        self,
        session_id: str,
        *,
        text: str,
        trigger: str,
        tokens_before: int,
        tokens_after: int,
        budget: int,
        artifacts: list[str] | None = None,
        pending: list[str] | None = None,
        errors: list[str] | None = None,
        source_run_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """写一版新卡片；version 由库里已有的最大值 +1 决定。"""
        if not text.strip():
            raise ValueError("中期卡片 text 不能为空")
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            version = repo.latest_version(conn, session_id) + 1
            return repo.save_memory_version(
                conn,
                session_id=session_id,
                version=version,
                trigger=trigger,
                text=text,
                artifacts=list(artifacts or []),
                pending=list(pending or []),
                errors=list(errors or []),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                budget=budget,
                source_run_ids=list(source_run_ids or []),
                ttl_days=self._policy.ttl_days,
                max_versions=self._policy.max_versions,
            )

    def versions(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            return repo.list_memory_versions(conn, session_id, limit)

    def delete(self, memory_id: int) -> bool:
        with repo.connect(self._db_path) as conn:
            repo.init_db(conn)
            return repo.delete_memory_version(conn, memory_id)
