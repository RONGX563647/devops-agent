"""收尾决策 —— 「这一轮怎么结束」的纯函数，以及它依赖的限额。

为什么要把决策抽出来：改造前它内嵌在 `_finish_or_park` 里，同一个函数既判断
「要不要挂起」、又写库（bump_asks / park）、又拼帧。结果是这条分支的测试必须
起一个假运行时 + 真 SQLite 才能跑（`test_ask_limit_auto_rejects` 就是这么写的）。

现在分成两半：
- 这里：纯函数，输入 (待确认调用, 已打扰次数, 限额)，输出一个 `Ending`；
- `run.py`：按 `Ending` 分派副作用（写库 / 拼帧）。

顺带修掉一处隐藏策略：旧代码在「确认次数读不到」时返回 `ASK_LIMIT + 1`，
把「库不可用」硬编码成「按超限处理」。现在它是显式配置项
`on_counter_unavailable`，默认仍是 reject（与历史行为一致），但换成 park 也只是一行 yaml。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from devops_agent.config import GatewayStreamPolicy


class Ending(StrEnum):
    """一轮 run 的三种结束方式。"""

    # 正常结束：写终态 + 发 event: done
    DONE = "done"
    # 挂起：正常结束这条流但不发 done，前端标「等待确认」
    PARKED = "parked"
    # 超出打扰上限：先自动拒绝让模型换方案，再重新判定一次
    OVER_ASK_LIMIT = "over_ask_limit"


@dataclass(frozen=True, slots=True)
class RunLimits:
    """一轮 run 的限额快照（来自 gateway.yaml，可注入）。"""

    ask_limit: int = 3
    park_ttl_seconds: int = 600
    settle_rounds: int = 2
    heartbeat_seconds: float = 15.0
    on_counter_unavailable: Literal["reject", "park"] = "reject"

    @classmethod
    def from_policy(cls, policy: GatewayStreamPolicy) -> RunLimits:
        """把配置对象翻成不可变快照：一轮之内限额不会中途变化。"""
        return cls(
            ask_limit=policy.ask_limit,
            park_ttl_seconds=policy.park_ttl_seconds,
            settle_rounds=policy.settle_rounds,
            heartbeat_seconds=policy.heartbeat_seconds,
            on_counter_unavailable=policy.on_counter_unavailable,
        )

    def expires_at(self, now: datetime | None = None) -> str:
        """挂起截止时刻（ISO 字符串，与 runs.expires_at 的存法一致）。"""
        moment = now or datetime.now(UTC)
        return (moment + timedelta(seconds=self.park_ttl_seconds)).isoformat()


def decide_ending(
    *,
    pending: Sequence[Mapping[str, Any]],
    asks: int | None,
    limits: RunLimits,
) -> Ending:
    """决定这一轮怎么结束。

    参数 `asks` 是**本次累加之后**的打扰次数（调用方负责 +1 后再传）；
    `None` 表示计数不可得（观测库不可用），此时按 `on_counter_unavailable` 处理。
    纯函数：不读时钟、不写库、不拼帧，所以可以直接表驱动测试。
    """
    if not pending:
        return Ending.DONE
    if asks is None:
        return Ending.OVER_ASK_LIMIT if limits.on_counter_unavailable == "reject" else Ending.PARKED
    return Ending.OVER_ASK_LIMIT if asks > limits.ask_limit else Ending.PARKED
