"""启动期回收：把上次进程留下的悬挂 run 收尾。

为什么放在启动期：悬挂的唯一来源是「进程没跑到 `finish()`」——进程被杀、崩溃、
`--reload` 重启都算。启动是唯一能确定「这些 run 已不可能再收尾」的时刻；放到
每次请求里做，只会让每次对话都多一次全表扫描。

为什么要有阈值：同一台机器上可能同时跑着多个进程共用一个库（前端 dev 起一个、
脚本评测起一个），刚创建几十秒的 run 属于别人正在跑的轮次，收尾它等于篡改别的
进程的观测数据。阈值留得比任何正常轮次都长。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from devops_agent.observability import repository as repo

#: 超过这个时长仍未收尾就判定为悬挂。正常一轮对话是秒级到分钟级，
#: 6 小时把「长任务」与「进程已经没了」彻底分开。
ORPHAN_AFTER_SECONDS = 6 * 3600

#: 落库的失败原因：可 grep，且能一眼区分「真失败」与「没跑完」
ORPHAN_REASON = "Orphaned: 进程重启，本轮未收尾"


def reap_orphan_runs(
    db_path: Path | None = None,
    *,
    older_than: int = ORPHAN_AFTER_SECONDS,
    now: datetime | None = None,
) -> int:
    """回收悬挂 run，返回条数；库不可用时返回 0（不让观测库问题拦住启动）。"""
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(seconds=older_than)).isoformat()
    try:
        with repo.connect(db_path) as conn:
            repo.init_db(conn)
            return repo.reap_orphan_runs(
                conn, started_before=cutoff, ended_at=moment.isoformat(), reason=ORPHAN_REASON
            )
    except Exception:  # noqa: BLE001 - 观测库坏了只影响观测，不能拦住服务启动
        return 0
