"""收尾决策的纯函数测试。

这一组用例替代了「起一个假运行时 + 真 SQLite 才能验上限」的旧写法：
`decide_ending` 不读时钟、不写库、不拼帧，所以可以表驱动。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from devops_agent.config import GatewayStreamPolicy
from devops_agent.gateway.ending import Ending, RunLimits, decide_ending

_PENDING = [{"tool_call_id": "c1", "name": "Bash", "input": "rm -rf dist", "risk": "danger"}]


@pytest.mark.parametrize(
    ("pending", "asks", "expected"),
    [
        # 没有待确认调用：无论打扰过几次都正常结束
        ([], None, Ending.DONE),
        ([], 9, Ending.DONE),
        # 第一次要求确认：挂起
        (_PENDING, 1, Ending.PARKED),
        # asks 是「本次累加之后」的值，上限 3 表示第 3 次仍然可以挂起
        (_PENDING, 3, Ending.PARKED),
        # 第 4 次才超限：自动拒绝，不再打扰用户
        (_PENDING, 4, Ending.OVER_ASK_LIMIT),
    ],
)
def test_decide_ending_table(
    pending: list[dict[str, str]], asks: int | None, expected: Ending
) -> None:
    limits = RunLimits(ask_limit=3)
    assert decide_ending(pending=pending, asks=asks, limits=limits) is expected


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("reject", Ending.OVER_ASK_LIMIT),
        ("park", Ending.PARKED),
    ],
)
def test_counter_unavailable_is_explicit_policy(policy: str, expected: Ending) -> None:
    """计数不可得（库不可用）时怎么处理必须显式：旧代码把它硬编码成「按超限」。

    默认 reject 保持历史行为；改成 park 只需一行 yaml，不必改代码。
    """
    limits = RunLimits(on_counter_unavailable=policy)  # type: ignore[arg-type]
    assert decide_ending(pending=_PENDING, asks=None, limits=limits) is expected


def test_run_limits_follow_deployment_policy() -> None:
    """限额来自 gateway.yaml：改配置即改行为，不需要 monkeypatch 模块常量。"""
    policy = GatewayStreamPolicy(
        ask_limit=1,
        park_ttl_seconds=90,
        settle_rounds=0,
        heartbeat_seconds=2.5,
        on_counter_unavailable="park",
    )
    limits = RunLimits.from_policy(policy)
    assert limits == RunLimits(
        ask_limit=1,
        park_ttl_seconds=90,
        settle_rounds=0,
        heartbeat_seconds=2.5,
        on_counter_unavailable="park",
    )


def test_expires_at_uses_injected_clock() -> None:
    """时钟可注入：挂起有效期是纯计算，测试不该依赖当前时间。"""
    moment = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert RunLimits(park_ttl_seconds=60).expires_at(moment) == "2026-01-01T00:01:00+00:00"
