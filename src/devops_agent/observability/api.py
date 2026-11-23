"""观测 API —— FastAPI 路由，读取 SQLite 中的运行数据供前端消费。

设计：
- 用 APIRouter 独立成模块，main.py 只做 include_router，避免入口膨胀；
- 每次请求用 repository.connect() 开短连接（WAL 下读不阻塞写），路径由环境变量决定，
  测试可通过 DEVOPS_AGENT_DB_PATH 覆盖；
- 响应是普通 dict / Pydantic 模型，不暴露内部对象。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from devops_agent.config import HealthPolicy, load_health
from devops_agent.observability import health as health_mod
from devops_agent.observability import repository as repo
from devops_agent.observability.models import EvalScoreRecord, RunRecord, SpanRecord

router = APIRouter(prefix="/api", tags=["observability"])


class RunOut(BaseModel):
    id: int
    session_id: str
    domain: str
    status: str
    input: str
    output: str
    started_at: str
    ended_at: str | None
    duration_ms: int | None
    model: str
    total_tokens: int
    health_score: float | None
    input_tokens: int
    output_tokens: int


class SpanOut(BaseModel):
    id: int
    run_id: int
    type: str
    name: str
    start_offset_ms: int
    duration_ms: int | None
    input: str
    output: str
    tokens: int
    status: str
    error: str | None


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int


class FeedbackIn(BaseModel):
    dimension: str = "correctness"
    score: float
    source: str = "human"
    rater: str = ""
    comment: str = ""


def _health_policy() -> HealthPolicy:
    return load_health()


@router.get("/runs", response_model=RunListOut)
def list_runs(
    domain: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RunListOut:
    """运行列表：按域/状态/时间窗筛选，分页倒序。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        rows = repo.query_runs(
            conn,
            domain=domain,
            status=status,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        total = repo.count_runs(conn, domain=domain, status=status, since=since, until=until)
    return RunListOut(
        items=[_run_out(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int) -> RunOut:
    """单次运行详情。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        run = repo.get_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_out(run)


@router.get("/runs/{run_id}/spans", response_model=list[SpanOut])
def get_spans(run_id: int) -> list[SpanOut]:
    """一次运行的步骤时间线。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        if repo.get_run(conn, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        spans = repo.list_spans(conn, run_id)
    return [_span_out(s) for s in spans]


@router.get("/runs/{run_id}/health")
def get_run_health(run_id: int) -> dict[str, object]:
    """单次运行的健康分与四类子分（实时计算）。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        run = repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        spans = repo.list_spans(conn, run_id)
        scores = repo.list_scores(conn, run_id)
    policy = _health_policy()
    return {
        "run_id": run_id,
        "score": health_mod.score_run(run, spans, scores, policy),
        "dimensions": {
            "execution": health_mod.execution_score(run, spans),
            "efficiency": health_mod.efficiency_score(run, policy.latency_baseline_ms),
            "quality": health_mod.quality_score(scores),
            "safety": health_mod.safety_score(spans),
        },
    }


@router.get("/metrics/summary")
def metrics_summary() -> dict[str, object]:
    """汇总指标：成功率、平均健康分、P95、token、成本代理值。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        runs = repo.summary_rows(conn)
        tool_total, tool_failed = repo.count_spans_by_type(conn, "tool")
    summary = health_mod.summarize(runs)
    summary["tool_call_count"] = tool_total
    summary["tool_failed_count"] = tool_failed
    return summary


@router.get("/metrics/timeseries")
def metrics_timeseries(days: int = Query(default=7, ge=1, le=90)) -> list[dict[str, object]]:
    """按天聚合的运行量与成功率趋势。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        runs = repo.summary_rows(conn)
    buckets: dict[str, list[str]] = {}
    for run in runs:
        day = run.started_at[:10]  # ISO 时间串的前 10 位即 YYYY-MM-DD
        buckets.setdefault(day, []).append(run.status)
    recent = sorted(buckets)[-days:]
    result: list[dict[str, object]] = []
    for day in recent:
        statuses = buckets[day]
        result.append(
            {
                "date": day,
                "total": len(statuses),
                "success": sum(1 for s in statuses if s == "success"),
                "failed": sum(1 for s in statuses if s == "failed"),
            }
        )
    return result


@router.post("/feedback/{run_id}", response_model=dict[str, object])
def post_feedback(run_id: int, feedback: FeedbackIn) -> dict[str, object]:
    """人工反馈写入 eval_scores（👍/👎 本质是 correctness 0/1）。"""
    if not 0.0 <= feedback.score <= 1.0:
        raise HTTPException(status_code=422, detail="score must be in [0, 1]")
    with repo.connect() as conn:
        repo.init_db(conn)
        if repo.get_run(conn, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        score = EvalScoreRecord(
            id=None,
            run_id=run_id,
            dimension=feedback.dimension,
            score=feedback.score,
            source=feedback.source,
            rater=feedback.rater,
            created_at=datetime.now(UTC).isoformat(),
            comment=feedback.comment,
        )
        score_id = repo.add_score(conn, score)
    return {"id": score_id, "run_id": run_id, "score": feedback.score}


@router.get("/eval/trends")
def eval_trends() -> dict[str, object]:
    """按维度与来源聚合评分趋势。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        scores = repo.all_scores(conn)
    by_dimension: dict[str, list[float]] = {}
    by_source: Counter[str] = Counter()
    for score in scores:
        by_dimension.setdefault(score.dimension, []).append(score.score)
        by_source[score.source] += 1
    averages = {dim: round(sum(values) / len(values), 4) for dim, values in by_dimension.items()}
    return {
        "count": len(scores),
        "averages": averages,
        "by_source": dict(by_source),
    }


def _run_out(run: RunRecord) -> RunOut:
    """RunRecord → RunOut（显式字段，避免把额外属性泄露给前端）。"""
    assert run.id is not None  # 从 DB 读出的记录必有 id
    return RunOut(
        id=run.id,
        session_id=run.session_id,
        domain=run.domain,
        status=run.status,
        input=run.input,
        output=run.output,
        started_at=run.started_at,
        ended_at=run.ended_at,
        duration_ms=run.duration_ms,
        model=run.model,
        total_tokens=run.total_tokens,
        health_score=run.health_score,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
    )


def _span_out(span: SpanRecord) -> SpanOut:
    """SpanRecord → SpanOut。"""
    assert span.id is not None
    return SpanOut(
        id=span.id,
        run_id=span.run_id,
        type=span.type,
        name=span.name,
        start_offset_ms=span.start_offset_ms,
        duration_ms=span.duration_ms,
        input=span.input,
        output=span.output,
        tokens=span.tokens,
        status=span.status,
        error=span.error,
    )
