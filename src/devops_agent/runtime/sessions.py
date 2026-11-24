"""会话隔离 —— 每个 session 一份 Agent 实例，状态快照落库。

为什么必须隔离（三条独立理由，任何一条都够）：
1. 规则串味：用户点「本会话允许」是往 `AgentState.permission_context.allow_rules`
   里加规则。单例 Agent 下，A 会话点一下，B 会话也放行了。
2. 挂起互斥：ASK 会把 tool_call 置为 ASKING 停在 state 里。两个会话同时挂起，
   `get_awaiting_tool_calls()` 返回的就是一锅粥，确认时无法分辨该答哪一个。
3. 历史串味（既有缺陷）：此前所有会话共享同一个 `Agent.state.context`。

**回收不等于丢失**：LRU 只淘汰内存里的实例，DB 里的 `session_states` 不动；
confirm 时缓存未命中就从 DB 重建。这样「挂起中的会话被回收」这个问题不需要
特殊豁免规则，也不依赖粘性会话 —— 请求落到哪个实例都能把 Agent 重建出来。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devops_agent.observability import repository as repo
from devops_agent.runtime.runtime import AgentRuntime

logger = logging.getLogger(__name__)

# 工厂：session_id + 档位 + 历史快照（JSON 或 None）+ 联网开关 → 运行时实例
RuntimeFactory = Callable[..., AgentRuntime]


def _now() -> datetime:
    return datetime.now(UTC)


def _effective_search(runtime: AgentRuntime, requested: bool | None) -> bool:
    """运行时解析后的联网状态；替身（Echo / 测试桩）没有该属性时退回入参。

    不能写成 bool(requested)：None 表示「按部署默认」，压成 False 会让
    `DEVOPS_AGENT_ENABLE_SEARCH=1` 的部署在会话重建时静默关掉检索。
    """
    effective = getattr(runtime, "web_search", None)
    if isinstance(effective, bool):
        return effective
    return bool(requested)


@dataclass
class SessionEntry:
    """一个会话的内存态。"""

    session_id: str
    runtime: AgentRuntime
    profile: str
    # 联网搜索会改变模型请求体，所以必须和档位一样纳入「变了就重建」的判断
    web_search: bool
    last_used: float
    # 同一会话同时只跑一个 run：SSE 流整段持有它，别让两个请求互相踩 state
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionRegistry:
    """session_id → SessionEntry，带 LRU 上限与 DB 快照重建。"""

    def __init__(
        self,
        factory: RuntimeFactory,
        *,
        db_path: Path | None = None,
        max_entries: int = 16,
    ) -> None:
        self._factory = factory
        self._db_path = db_path
        self._max = max(1, max_entries)
        self._entries: OrderedDict[str, SessionEntry] = OrderedDict()

    # --- 内部 ---------------------------------------------------------
    def _load_state(self, session_id: str) -> str | None:
        """从库里取会话快照；库不可用就当新会话，不让存储故障挡住对话。"""
        try:
            with repo.connect(self._db_path) as conn:
                repo.init_db(conn)
                saved = repo.load_session_state(conn, session_id)
        except Exception:  # noqa: BLE001 - 存储故障不该阻断对话
            logger.warning("读取会话快照失败: %s", session_id, exc_info=True)
            return None
        return saved[0] if saved and saved[0] else None

    def _evict(self) -> None:
        """超上限时淘汰最久未用且不在运行中的会话。"""
        while len(self._entries) > self._max:
            victim = next(
                (key for key, entry in self._entries.items() if not entry.lock.locked()),
                None,
            )
            if victim is None:
                return  # 全都在跑，宁可超限也不打断正在进行的会话
            self._entries.pop(victim, None)
            logger.debug("会话实例被回收: %s", victim)

    def _build(self, session_id: str, profile: Any, web_search: bool | None) -> SessionEntry:
        state_json = self._load_state(session_id)
        runtime = self._factory(session_id, profile, state_json, web_search)
        entry = SessionEntry(
            session_id=session_id,
            runtime=runtime,
            profile=str(getattr(profile, "key", "")),
            # 记「实际生效值」而不是入参：入参 None 表示按部署默认，
            # 交给运行时解析后才知道到底联没联网，照抄 None 会让开关比较失真。
            web_search=_effective_search(runtime, web_search),
            last_used=_now().timestamp(),
        )
        return entry

    # --- 对外 ---------------------------------------------------------
    def acquire(
        self, session_id: str, profile: Any, web_search: bool | None = None
    ) -> SessionEntry:
        """取得会话实例：命中复用；档位或联网开关变了重建；未命中从 DB 快照重建。

        web_search=None 表示「不关心」：确认/续跑沿用现有实例的配置，
        否则前端没带这个字段就会把挂起中的会话重建掉。
        """
        entry = self._entries.get(session_id)
        key = str(getattr(profile, "key", ""))
        if (
            entry is not None
            and entry.profile == key
            and (web_search is None or entry.web_search == web_search)
        ):
            entry.last_used = _now().timestamp()
            self._entries.move_to_end(session_id)
            return entry
        if entry is not None:
            # 切换档位/开关：先把旧状态存下来，再用新配置重建同一个会话
            self.save(session_id)
            self._entries.pop(session_id, None)
        fresh = self._build(session_id, profile, web_search)
        self._entries[session_id] = fresh
        self._evict()
        return fresh

    def peek(self, session_id: str) -> SessionEntry | None:
        """取当前实例但不重建、不碰档位：切档端点专用。

        `acquire()` 的语义是「按这次请求的档位拿到一个实例」，档位不同就重建；
        运行中切档不能走它，否则正在跑的流会被换掉。
        """
        entry = self._entries.get(session_id)
        if entry is not None:
            entry.last_used = _now().timestamp()
            self._entries.move_to_end(session_id)
        return entry

    def switch(
        self,
        session_id: str,
        profile: Any,
        web_search: bool | None = None,
    ) -> tuple[SessionEntry, bool]:
        """会话中切档：能热替换就原地改，返回 (entry, applied)。

        applied=False 表示运行时没有热替换能力（Echo / 测试替身），
        已按旧路径重建；没有实例时也从 DB 快照建一份给后续轮次用。
        两种情况下调用方都要如实告知用户生效范围，不能一律说「立即生效」。
        """
        entry = self.peek(session_id)
        if entry is None:
            return self.acquire(session_id, profile, web_search), False
        apply_profile = getattr(entry.runtime, "apply_profile", None)
        if not callable(apply_profile):
            self._entries.pop(session_id, None)
            return self.acquire(session_id, profile, web_search), False
        apply_profile(profile, web_search=web_search)
        entry.profile = str(getattr(profile, "key", ""))
        if web_search is not None:
            entry.web_search = web_search
        # 快照里的档位也一起更新：刷新后重建要按新档装配，而不是退回发起轮
        self.save(session_id)
        self._evict()
        return entry, True

    def save(self, session_id: str) -> None:
        """把当前会话快照写回库里；运行时没有快照能力（如 Echo）则跳过。"""
        entry = self._entries.get(session_id)
        if entry is None:
            return
        snapshot = getattr(entry.runtime, "snapshot", None)
        if not callable(snapshot):
            return
        try:
            payload = str(snapshot())
        except Exception:  # noqa: BLE001 - 序列化失败不该让请求失败
            logger.warning("序列化会话状态失败: %s", session_id, exc_info=True)
            return
        try:
            with repo.connect(self._db_path) as conn:
                repo.init_db(conn)
                repo.save_session_state(
                    conn,
                    session_id,
                    state_json=payload,
                    permission=entry.profile,
                    updated_at=_now().isoformat(),
                )
        except Exception:  # noqa: BLE001
            logger.warning("写入会话快照失败: %s", session_id, exc_info=True)

    def drop(self, session_id: str) -> None:
        """丢弃内存态（不删库里的快照）。"""
        self._entries.pop(session_id, None)

    @property
    def sessions(self) -> list[str]:
        return list(self._entries)
