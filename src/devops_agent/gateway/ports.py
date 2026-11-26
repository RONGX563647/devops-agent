"""两个端口：运行时可选能力、挂起态存储。

它们存在的理由相同：把「不属于网关的知识」挡在网关之外。

1. `RuntimePort` —— 运行时协议（`AgentRuntime`）只强制 `stream`，其余能力
   （`resume` / `pending_calls` / `snapshot` / `apply_profile` / `model_params_record` /
   `web_search`）都是 Duck typing。改造前这些探测散在 9 处
   （`getattr(x, "resume", None)` + `callable()`），于是「Echo 有什么、没什么、
   没有时语义是什么」只写在注释里。现在收敛成一处：缺能力给 Null 行为，
   唯一的布尔 `resumable` 决定错误话术。
2. `PendingStore` —— 挂起态的读写。改造前 `confirm.py` 直接拿 sqlite 行 dict，
   用 `row["parked"]` / `row["pending_json"]` 这样的字符串键访问列名，
   于是「验证归属 / 判断过期 / 解挂幂等」这类业务判断被钉死在列形状上。
   现在行形状只在本模块出现，对外是 `PendingRun`。

两个端口都保持协议兼容：任何实现了旧方法的运行时/替身都不需要改。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from devops_agent.gateway.contract import PendingPayload
from devops_agent.observability import repository as repo
from devops_agent.runtime.confirm import ConfirmDecision
from devops_agent.runtime.runtime import AgentRuntime


class RuntimeCapabilityError(RuntimeError):
    """运行时没有这项可选能力，而调用方跳过了 `resumable` 判断。

    抛错而不是静默返回空流：空流会让「续跑什么都没发生」表现成模型答非所问。
    """


class RuntimePort:
    """`AgentRuntime` 的可选能力适配器（Null Object）。

    构造时一次性探测，之后每次调用只走显式分支：不会出现「第一次 supported、
    第二次 not」这种按调用漂移的行为。
    """

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime
        # 探测集中在构造函数：改运行时实现时只需读这一屏就知道缺什么
        self._resume = getattr(runtime, "resume", None)
        self._pending = getattr(runtime, "pending_calls", None)
        self._snapshot = getattr(runtime, "snapshot", None)
        self._params = getattr(runtime, "model_params_record", None)
        self._web_search = getattr(runtime, "web_search", None)

    @property
    def runtime(self) -> AgentRuntime:
        """底层运行时（需要专用能力时显式取用，网关其余部分不该碰它）。"""
        return self._runtime

    @property
    def resumable(self) -> bool:
        """能否从挂起点续跑。Echo / 只实现 stream 的替身为 False。"""
        return callable(self._resume)

    def pending(self) -> list[dict[str, Any]]:
        """当前仍等待确认的调用；没有这能力就是空列表（等价于「没有挂起」）。"""
        if not callable(self._pending):
            return []
        return [dict(item) for item in self._pending()]

    def resume(self, decisions: Sequence[ConfirmDecision]) -> AsyncIterator[dict[str, Any]]:
        """带决策续跑；不支持时抛 `RuntimeCapabilityError`（调用方应先看 resumable）。"""
        if not callable(self._resume):
            raise RuntimeCapabilityError("当前运行时没有续跑能力（只实现了 stream）")
        stream: AsyncIterator[dict[str, Any]] = self._resume(decisions)
        return stream

    def snapshot(self) -> str | None:
        """会话状态快照；没有这能力返回 None（调用方跳过落库，不当成空快照覆盖）。"""
        if not callable(self._snapshot):
            return None
        value: str = self._snapshot()
        return value

    def params_record(self) -> dict[str, Any]:
        """本轮实际下发的模型参数快照（只含非默认值）；取不到返回空 dict。"""
        if not callable(self._params):
            return {}
        record: dict[str, Any] = self._params()
        return record

    def web_search(self) -> bool | None:
        """运行时解析后的联网状态；`None` 表示该运行时没有这个概念（Echo）。"""
        value = self._web_search
        return value if isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class PendingRun:
    """一条挂起中的 run（对外形状，不暴露数据库列名）。"""

    run_id: int
    session_id: str
    parked: bool
    expires_at: str
    permission: str
    payload: PendingPayload

    def owned_by(self, session_id: str) -> bool:
        """归属校验：只凭 run_id 就能读到别人待执行的命令原文，等于把破坏性操作当公开信息。"""
        return self.session_id == session_id

    def is_expired(self, now: str) -> bool:
        """是否已过期。空 expires_at 视为「永不过期」（兼容没有该列的老记录）。"""
        return bool(self.expires_at) and self.expires_at <= now

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """待确认调用的展示项（命令原文以服务端这份为权威）。"""
        return self.payload.tool_calls


class PendingStore:
    """挂起态读写（当前实现落 SQLite；网关只依赖这里的方法签名）。

    所有状态迁移都是「带前置状态的条件 UPDATE」：影响 0 行即说明状态已被别人改过，
    调用方据此返回 409 —— 双击「允许一次」、两个标签页同时确认都只会执行一次。
    """

    def load(self, run_id: int) -> PendingRun | None:
        """读挂起记录；不存在返回 None（由调用方映射成 404）。"""
        with repo.connect() as conn:
            repo.init_db(conn)
            row = repo.get_pending(conn, run_id)
        if row is None:
            return None
        return PendingRun(
            run_id=int(str(row["run_id"])),
            session_id=str(row["session_id"]),
            parked=bool(row["parked"]),
            expires_at=str(row["expires_at"] or ""),
            permission=str(row["permission"] or ""),
            payload=PendingPayload.from_json(_as_text(row["pending_json"])),
        )

    def resolve(self, run_id: int, *, now: str) -> bool:
        """解除挂起（幂等）：只有把 parked 从 1 翻成 0 的那个请求拿到 True。

        过期判定与状态迁移在同一条 SQL 里完成，避免「先查再改」的竞态窗口。
        """
        with repo.connect() as conn:
            repo.init_db(conn)
            return repo.unpark_run(conn, run_id, now=now)

    def bump_asks(self, run_id: int | None) -> int | None:
        """本 run 的打扰次数 +1；`None` 表示计数不可得（没有 run 或库不可用）。

        返回 None 而不是「上限 + 1」：不可得是一个事实，怎么处理是策略（见 ending.py）。
        """
        if run_id is None:
            return None
        with repo.connect() as conn:
            repo.init_db(conn)
            return repo.bump_asks(conn, run_id)


def _as_text(value: object) -> str | None:
    """把列值收敛成「字符串或 None」：老库可能存 NULL，JSON 解析侧只认这两种。"""
    return None if value is None else str(value)
