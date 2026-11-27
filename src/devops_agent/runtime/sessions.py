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

from devops_agent import model_settings
from devops_agent.observability import repository as repo
from devops_agent.runtime.runtime import AgentRuntime

logger = logging.getLogger(__name__)

# 工厂：session_id + 档位 + 历史快照（JSON 或 None）+ 联网开关 + 参数覆盖 → 运行时实例
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
    # 该会话的模型参数覆盖（DB 里那份的镜像）：改部署默认时要拿它重算
    model_overrides: dict[str, Any] = field(default_factory=dict)
    # 绑定工作区（SPEC-0021）：id 决定长期记忆作用域，ref 保存 root_path 供重建
    workspace_id: str = ""
    workspace_ref: Any | None = None
    # 部署默认变了但这个会话正在跑：先挂起，run 结束后再热替换。
    # 直接广播会打断正在进行的流，只影响新会话又等于「改了但没生效」。
    pending_model: bool = False


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

    def _load_model_config(self, session_id: str) -> dict[str, Any]:
        """从库里取该会话的参数覆盖；库不可用当作无覆盖（回落部署默认）。"""
        try:
            with repo.connect(self._db_path) as conn:
                repo.init_db(conn)
                return repo.load_session_model_config(conn, session_id)
        except Exception:  # noqa: BLE001 - 存储故障不该挡住对话
            logger.warning("读取会话参数覆盖失败: %s", session_id, exc_info=True)
            return {}

    def _build(
        self,
        session_id: str,
        profile: Any,
        web_search: bool | None,
        workspace: Any | None = None,
    ) -> SessionEntry:
        state_json = self._load_state(session_id)
        overrides = self._load_model_config(session_id)
        if workspace is None:
            # 未绑定工作区：保持旧工厂签名，Echo / 测试替身无需感知新参数
            runtime = self._factory(
                session_id, profile, state_json, web_search, model_config=overrides
            )
        else:
            runtime = self._factory(
                session_id,
                profile,
                state_json,
                web_search,
                model_config=overrides,
                workspace=workspace,
            )
        entry = SessionEntry(
            session_id=session_id,
            runtime=runtime,
            profile=str(getattr(profile, "key", "")),
            # 记「实际生效值」而不是入参：入参 None 表示按部署默认，
            # 交给运行时解析后才知道到底联没联网，照抄 None 会让开关比较失真。
            web_search=_effective_search(runtime, web_search),
            last_used=_now().timestamp(),
            model_overrides=overrides,
            workspace_id=str(getattr(workspace, "id", "") or "") if workspace is not None else "",
            workspace_ref=workspace,
        )
        return entry

    # --- 对外 ---------------------------------------------------------
    def acquire(
        self,
        session_id: str,
        profile: Any,
        web_search: bool | None = None,
        *,
        workspace: Any | None = None,
    ) -> SessionEntry:
        """取得会话实例：命中复用；档位/联网开关/工作区变了重建；未命中从 DB 快照重建。

        web_search=None 表示「不关心」：确认/续跑沿用现有实例的配置，
        否则前端没带这个字段就会把挂起中的会话重建掉。
        `workspace=None` 表示「不改归属」：有活跃实例时沿用它的工作区，
        没有实例时回落到旧路径（DEVOPS_AGENT_WORKDIR）。
        """
        entry = self._entries.get(session_id)
        key = str(getattr(profile, "key", ""))
        requested_id = str(getattr(workspace, "id", "") or "") if workspace is not None else None
        workspace_ok = requested_id is None or (
            entry is not None and entry.workspace_id == requested_id
        )
        if (
            entry is not None
            and entry.profile == key
            and workspace_ok
            and (web_search is None or entry.web_search == web_search)
        ):
            entry.last_used = _now().timestamp()
            self._entries.move_to_end(session_id)
            return entry
        target = (
            workspace
            if workspace is not None
            else (entry.workspace_ref if entry is not None else None)
        )
        if entry is not None:
            # 切换档位/开关/工作区：先把旧状态存下来，再用新配置重建同一个会话
            self.save(session_id)
            self._entries.pop(session_id, None)
        fresh = self._build(session_id, profile, web_search, target)
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
        *,
        workspace: Any | None = None,
    ) -> tuple[SessionEntry, bool]:
        """会话中切档：能热替换就原地改，返回 (entry, applied)。

        applied=False 表示运行时没有热替换能力（Echo / 测试替身），
        已按旧路径重建；没有实例时也从 DB 快照建一份给后续轮次用。
        两种情况下调用方都要如实告知用户生效范围，不能一律说「立即生效」。
        """
        entry = self.peek(session_id)
        if entry is None:
            return self.acquire(session_id, profile, web_search, workspace=workspace), False
        apply_profile = getattr(entry.runtime, "apply_profile", None)
        if not callable(apply_profile):
            self._entries.pop(session_id, None)
            return self.acquire(
                session_id,
                profile,
                web_search,
                workspace=workspace if workspace is not None else entry.workspace_ref,
            ), False
        apply_profile(profile, web_search=web_search)
        entry.profile = str(getattr(profile, "key", ""))
        if web_search is not None:
            entry.web_search = web_search
        # 快照里的档位也一起更新：刷新后重建要按新档装配，而不是退回发起轮
        self.save(session_id)
        self._evict()
        return entry, True

    # --- 模型参数 -----------------------------------------------------
    def _apply_entry(self, entry: SessionEntry) -> bool:
        """把「该会话当前应有的参数」推给运行时；Echo 之类没有热替换能力时返回 False。"""
        apply = getattr(entry.runtime, "apply_model_params", None)
        if not callable(apply):
            return False
        apply(model_settings.effective_params(session=entry.model_overrides))
        return True

    def apply_model_params(self, session_id: str, overrides: dict[str, Any]) -> bool:
        """热替换某个会话的参数覆盖；没有活跃实例或不支持热替换时返回 False。

        返回 False 时调用方要说「下一轮生效」—— 不能一律说成立即生效，
        这是切档端点已经建立的约定。
        """
        entry = self.peek(session_id)
        if entry is None:
            return False
        entry.model_overrides = dict(overrides)
        return self._apply_entry(entry)

    def broadcast_model_defaults(self) -> dict[str, int]:
        """部署默认改了：遍历活跃会话，逐个判断能不能立刻热替换。

        返回 `{"applied": n, "deferred": m}`，前端据此说「n 个已更新，
        m 个等当前轮结束」。正在跑的会话不打断，只记 pending，run 结束后补上。
        """
        counts = {"applied": 0, "deferred": 0}
        for entry in self._entries.values():
            if entry.lock.locked():
                entry.pending_model = True
                counts["deferred"] += 1
            elif self._apply_entry(entry):
                counts["applied"] += 1
            else:
                counts["deferred"] += 1
        return counts

    def flush_pending(self, session_id: str) -> bool:
        """一轮结束后应用挂起的部署默认；返回是否真的应用了。"""
        entry = self._entries.get(session_id)
        if entry is None or not entry.pending_model or entry.lock.locked():
            return False
        # 先应用、成功后才清标记：_apply_entry 失败或抛异常时若先清标记，
        # 这一轮的部署默认就永久丢了，下一次 flush 也不会再补。
        if not self._apply_entry(entry):
            logger.warning("会话 %s 的挂起参数应用失败，下轮再试", session_id)
            return False
        entry.pending_model = False
        return True

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


def registry_for(request: Any) -> SessionRegistry:
    """取请求所属 app 的会话注册表，首次访问时按当前 app.state.runtime 建工厂。

    注册表与「它基于哪个 runtime 实例」一起记住：测试 / 部署替换
    app.state.runtime 后必须重建，否则会一直用旧实例的工厂。
    放在本模块而不是 main：模型配置端点也要用同一份注册表，
    让 main 反向被 import 会绕成环。
    """
    from devops_agent.runtime.assembly import session_runtime_factory  # 避开循环 import

    base = getattr(request.app.state, "runtime", None)
    registry = getattr(request.app.state, "sessions", None)
    if registry is None or getattr(request.app.state, "sessions_base", None) is not base:
        registry = SessionRegistry(session_runtime_factory(base))
        request.app.state.sessions = registry
        request.app.state.sessions_base = base
    return registry
