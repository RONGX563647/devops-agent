"""会话快照的版本戳与恢复计数（SPEC-0013 §18.2 M5）。

两件事必须落在这里，而不是散在运行时里：

1. **版本戳**：`/api/context` 读 `middle_context["snapshot_version"]`，
   但此前没有任何地方写它，接口永远显示 1，版本迁移逻辑等于空转。
2. **恢复失败的可见性**：快照损坏时退回新会话是正确降级，但一声不响地
   丢掉整段对话历史是排障事故 —— 计数要能被 `/health` 看到。

不引入 `CONTEXT_RESTORE` 事件：§8.3 已规定它不进 SSE，恢复发生在装配期，
那时还没有流可以承载事件。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 写进 `middle_context` 的键名；与 `tools/context_api.py` 的读取侧必须一致。
SNAPSHOT_VERSION_KEY = "snapshot_version"

# 进程级计数：只在装配/恢复路径上自增，读侧是 /health
_stats: dict[str, Any] = {"restore_failures": 0, "last_error": ""}


def stamp_snapshot(state: Any, version: int) -> None:
    """把快照版本号写进状态；state 没有 middle_context 时静默跳过（测试替身）。"""
    middle = getattr(state, "middle_context", None)
    if not isinstance(middle, dict):
        return
    middle[SNAPSHOT_VERSION_KEY] = int(version)
    state.middle_context = middle


def record_restore_failure(session_id: str, exc: Exception) -> None:
    """记录一次「快照读不出来、已按新会话处理」。

    等级用 warning 而不是 error：对话仍然能用，只是历史丢了。
    真正的 error 应该留给「会话建不起来」那种情况。
    """
    _stats["restore_failures"] += 1
    _stats["last_error"] = f"{type(exc).__name__}: {exc}"
    logger.warning(
        "会话快照恢复失败，按新会话处理: session=%s error=%s: %s",
        session_id,
        type(exc).__name__,
        exc,
        exc_info=True,
    )


def restore_stats() -> dict[str, Any]:
    """给 `/health` 用的只读快照。"""
    return dict(_stats)


def reset_restore_stats() -> None:
    """测试用：清零计数，避免用例之间互相污染。"""
    _stats["restore_failures"] = 0
    _stats["last_error"] = ""
