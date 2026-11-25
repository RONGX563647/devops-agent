"""在线评测的任务生命周期（HTTP 入口用）。

三条约束，都是刻意的：

1. 单进程 asyncio 任务，不引入队列。本项目是单机单用户工具（ADR-0001），
   加 Celery/Redis 是过度设计；代价是进程重启即丢任务，由 `reap_stale_runs`
   把残留标成 failed，而不是假装还能续跑。
2. 单飞锁在 DB（`oj_runs` 的部分唯一索引），不在内存。uvicorn 开多 worker 时
   内存标志位等于没有锁。
3. 进度按用例落库。轮询接口读库就能拿到 `done`，不依赖进程内状态；
   只有 `current_case` 是内存值（它每 2 秒被覆盖，丢了也不影响结论）。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devops_agent.openjudge import benchmark, grade, run, run_store, store

ENV_FLAG = "DEVOPS_AGENT_OPENJUDGE_ONLINE"
REPO_ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = REPO_ROOT / "eval" / "openjudge" / "cases"

MAX_LIMIT = 30
DEFAULT_LIMIT = 10
MAX_CONCURRENCY = 4

_current: dict[int, str] = {}
_tasks: dict[int, asyncio.Task[None]] = {}
_cancelled: set[int] = set()


def online_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def load_cases(track: str = "", limit: int = DEFAULT_LIMIT) -> list[benchmark.Case]:
    """按赛道与条数上限取用例。track 为空表示全部赛道。"""
    paths = sorted(CASES_DIR.glob("*.jsonl"))
    cases = benchmark.load_cases(paths)
    if track:
        cases = [case for case in cases if case.track == track]
    if limit:
        cases = cases[:limit]
    return cases


async def start(*, track: str, limit: int, judge: bool, concurrency: int) -> dict[str, Any]:
    """占用运行槽并起后台任务。

    1. 开关、赛道、用例非空先校验 —— 这些都是调用方错误（400），不是服务故障；
    2. 打开库、清掉重启留下的幽灵任务，再抢单飞槽；抢不到就返回 `busy`；
    3. 抢到后登记进度、起 asyncio 任务，请求立即返回 `run_id` 与总数。

    返回 `{"run_id", "total"}`；已有任务时返回 `{"busy": run_id}`（由接口层转 409）。
    """
    if not online_enabled():
        raise RuntimeError(f"在线评测未开启（设置 {ENV_FLAG}=1）")
    if track and track not in benchmark.TRACKS:
        raise ValueError(f"未知赛道：{track}")
    cases = load_cases(track, limit)
    if not cases:
        raise ValueError("没有可执行的用例")
    workers = max(1, min(concurrency, MAX_CONCURRENCY))
    with closing(store.connect()) as conn:
        store.init_db(conn)
        run_store.reap_stale_runs(conn)
        run_id = run_store.claim_run(
            conn, track=track, total=len(cases), judge=judge, concurrency=workers
        )
        if run_id is None:
            active = run_store.active_run(conn)
            return {"busy": int(active["id"]) if active else None, "total": 0}
    _current[run_id] = ""
    _tasks[run_id] = asyncio.create_task(_execute(run_id, cases, judge=judge, concurrency=workers))
    return {"run_id": run_id, "total": len(cases)}


async def _execute(
    run_id: int, cases: list[benchmark.Case], *, judge: bool, concurrency: int
) -> None:
    """后台跑一条流水线，把结果落成任务终态。

    1. 每完成一条就通过 `progress` 写库，轮询接口只读库，不依赖进程内状态；
    2. 取消 / 配置错误 / 其他异常分别落成 cancelled / failed，错误文案带类型；
    3. 收尾清掉内存里的进度与任务句柄，避免长跑进程越攒越多。
    """

    def progress(done: int, total: int, case_id: str) -> None:
        _current[run_id] = case_id
        with closing(store.connect()) as conn:
            run_store.update_progress(conn, run_id, done)

    opts = run.RunOptions(judge=judge, concurrency=concurrency)
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
