"""在线评测的任务生命周期（HTTP 入口用）。

三条约束，都是刻意的：

1. 单进程 asyncio 无队列，重启丢任务标 failed 2. 单飞锁在 DB 不在内存 3. 进度按用例落库供轮询。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from devops_agent.openjudge import benchmark, casestore, grade, run, run_store, settings, store

ENV_FLAG = "DEVOPS_AGENT_OPENJUDGE_ONLINE"

MAX_LIMIT = 30
DEFAULT_LIMIT = 10
MAX_CONCURRENCY = settings.MAX_CONCURRENCY

_current: dict[int, str] = {}
_tasks: dict[int, asyncio.Task[None]] = {}
_cancelled: set[int] = set()


def online_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def load_cases(track: str = "", limit: int = DEFAULT_LIMIT) -> list[benchmark.Case]:
    """按赛道与条数上限取用例（含本地覆盖层，见 `casestore`）。"""
    return casestore.load_cases(track=track, limit=limit)


async def start(
    *,
    track: str,
    limit: int,
    judge: bool | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """占用运行槽并起后台任务。

    `judge` / `concurrency` 省略时取界面里存的默认值（`oj_settings`）——「点一下就
    跑」和「按上次配置再跑」必须用同一套参数，否则两次结果没法比。

    返回 `{"run_id", "total"}`；已有任务时返回 `{"busy": run_id}`（由接口层转 409）。
    """
    # step1: 校验开关/赛道/用例非空
    if not online_enabled():
        raise RuntimeError(f"在线评测未开启（设置 {ENV_FLAG}=1）")
    if track and track not in benchmark.TRACKS:
        raise ValueError(f"未知赛道：{track}")
    defaults = settings.load()
    cases = load_cases(track, limit)
    if not cases:
        raise ValueError("没有可执行的用例")
    active_judge = bool(defaults["judge"]) if judge is None else judge
    requested = int(defaults["concurrency"]) if concurrency is None else concurrency
    workers = max(1, min(requested, MAX_CONCURRENCY))
    # step2: 清幽灵任务 + 抢 DB 单飞槽
    with closing(store.connect()) as conn:
        store.init_db(conn)
        run_store.reap_stale_runs(conn)
        run_id = run_store.claim_run(
            conn, track=track, total=len(cases), judge=active_judge, concurrency=workers
        )
        if run_id is None:
            active = run_store.active_run(conn)
            return {"busy": int(active["id"]) if active else None, "total": 0}
    # step3: 起 asyncio 任务，立即返回 run_id
    _current[run_id] = ""
    _tasks[run_id] = asyncio.create_task(
        _execute(
            run_id,
            cases,
            judge=active_judge,
            concurrency=workers,
            timeout=float(defaults["timeout"]),
        )
    )
    return {"run_id": run_id, "total": len(cases)}


async def _execute(
    run_id: int,
    cases: list[benchmark.Case],
    *,
    judge: bool,
    concurrency: int,
    timeout: float = 180.0,
) -> None:
    """后台跑一条流水线，把结果落成任务终态。"""

    # step1: 每条完成经 progress 写库，轮询只读库
    def progress(done: int, total: int, case_id: str) -> None:
        _current[run_id] = case_id
        with closing(store.connect()) as conn:
            run_store.update_progress(conn, run_id, done)

    opts = run.RunOptions(judge=judge, concurrency=concurrency, timeout=timeout)
    # step2: 取消/配置错/其余异常分别落终态
    try:
        result = await run.execute(
            cases,
            options=opts,
            progress=progress,
            should_stop=lambda: run_id in _cancelled,
        )
    except run.RunCancelled:
        _finish(run_id, status="cancelled")
    except grade.ConfigError as exc:
        _finish(run_id, status="failed", error=f"配置错误：{exc}")
    except Exception as exc:  # noqa: BLE001 - 后台任务的异常必须有落点
        _finish(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
    else:
        _finish(run_id, status="done", batch_id=result.batch_id)
    # step3: 收尾清内存进度与任务句柄
    finally:
        _current.pop(run_id, None)
        _cancelled.discard(run_id)
        _tasks.pop(run_id, None)


def _finish(run_id: int, *, status: str, batch_id: int | None = None, error: str = "") -> None:
    with closing(store.connect()) as conn:
        run_store.finish_run(conn, run_id, status=status, batch_id=batch_id, error=error)


def cancel(run_id: int) -> dict[str, Any] | None:
    """请求取消。已开始的那条用例不打断，收尾后状态由 `_execute` 落定。"""
    with closing(store.connect()) as conn:
        row = run_store.get_run(conn, run_id)
        if row is None:
            return None
        if row["status"] in {"pending", "running"}:
            _cancelled.add(run_id)
            run_store.finish_run(conn, run_id, status="cancelled")
        updated = run_store.get_run(conn, run_id)
    return _decorate(updated) if updated else None


def status(run_id: int) -> dict[str, Any] | None:
    with closing(store.connect()) as conn:
        row = run_store.get_run(conn, run_id)
    return _decorate(row) if row else None


def recent(limit: int = 10) -> list[dict[str, Any]]:
    with closing(store.connect()) as conn:
        store.init_db(conn)
        rows = run_store.list_runs(conn, limit=limit)
    return [_decorate(row) for row in rows]


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    row["current_case"] = _current.get(int(row["id"]), "")
    row["elapsed_ms"] = _elapsed_ms(row)
    return row


def _elapsed_ms(row: dict[str, Any]) -> int:
    try:
        started = datetime.fromisoformat(str(row.get("started_at", "")))
    except ValueError:
        return 0
    raw = str(row.get("finished_at", "") or "")
    try:
        end = datetime.fromisoformat(raw) if raw else datetime.now(UTC)
    except ValueError:
        end = datetime.now(UTC)
    return max(0, int((end - started).total_seconds() * 1000))
