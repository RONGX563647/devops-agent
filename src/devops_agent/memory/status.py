"""进程级记忆自述 —— 回答「现在是什么状态、表里有多少条」。

单独一层的理由：这些函数只读配置与库、不参与一轮对话的读写路径，放在
`runtime.py` 里会把那个文件顶到单文件行数上限，也会让「一次对话要加载什么」
和「一个探针要加载什么」混在一起。

字段分工：
- 开关（enabled / backend）说「打算做什么」；
- `store` 条数说「真的存下来了没有」—— 线上最常见的困惑是「配好了 local、
  后端也说生效，但记忆一条没有」，只看开关无法区分这两种状态。
"""

from __future__ import annotations

from typing import Any

from devops_agent.config import load_memory
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.scope import resolve_scope_id
from devops_agent.memory.sink import MemorySink
from devops_agent.observability import repository as repo


def memory_status() -> dict[str, Any]:
    """进程级记忆状态（/health 与 /api/memory/status 共用）。"""
    policy = load_memory()
    runtime = MemoryRuntime("", policy, MemorySink(), None)
    state = runtime.status()
    state["store"] = store_counts()
    state["policy"] = {
        "short_term_ratio": policy.budget.short_term_ratio,
        "episodic_ratio": policy.budget.episodic_ratio,
        "long_term_ratio": policy.budget.long_term_ratio,
        "headroom_ratio": policy.budget.headroom_ratio,
    }
    return state


def store_counts() -> dict[str, int]:
    """两层表里的实际条数；读不到时全 0（观测库坏了不该让 /health 500）。"""
    try:
        with repo.connect() as conn:
            repo.init_db(conn)
            return repo.store_counts(conn)
    except Exception:  # noqa: BLE001 - 计数失败只影响自述，不影响记忆读写
        return {"episodic": 0, "long_term": 0}


def session_counts(session_id: str) -> dict[str, int]:
    """某个会话的两层条数（管理接口用）；读不到时返回 0。"""
    policy = load_memory()
    scope_id = resolve_scope_id(policy, session_id)
    try:
        with repo.connect() as conn:
            repo.init_db(conn)
            return {
                "versions": len(repo.list_memory_versions(conn, session_id, 100)),
                "long_term": repo.count_long_term(conn, policy.long_term.scope, scope_id),
            }
    except Exception:  # noqa: BLE001
        return {"versions": 0, "long_term": 0}
