"""观测 API —— FastAPI 路由，读取 SQLite 中的运行数据供前端消费。

设计：
- 用 APIRouter 独立成模块，gateway/app.py 只做 include_router，避免入口膨胀；
- 每次请求用 repository.connect() 开短连接（WAL 下读不阻塞写），路径由环境变量决定，
  测试可通过 DEVOPS_AGENT_DB_PATH 覆盖；
- 响应是普通 dict / Pydantic 模型，不暴露内部对象。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from devops_agent.config import HealthPolicy, load_health
from devops_agent.eval.scorer import EvalDimension
from devops_agent.observability import health as health_mod
from devops_agent.observability import repository as repo
from devops_agent.observability.models import (
    EvalScoreRecord,
    InputStageRecord,
    RunRecord,
    SpanRecord,
)

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
    # 前缀缓存命中/写入量；老记录为 0，前端据此显示「无缓存数据」而不是 0%
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # 本轮权限档位；老记录为空串
    permission: str = ""
    # 双口径耗时：work_ms 是 span 并集（真正在干活），wall_ms 是起止差（含用户空闲）
    work_ms: int = 0
    wall_ms: int = 0
    # 本轮实际下发的模型参数（JSON，只含非默认值）；空串 = 老记录，显示「未记录」
    model_params: str = ""


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
    # 逐调用的 token 分拆：模型 span 才有值，工具 span 保持 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # run 内单调序号与父 span：泳道与列表共用，工具可追溯到发起它的模型调用
    seq: int = 0
    parent_id: int | None = None


class InputStageOut(BaseModel):
    seq: int
    stage: str
    label: str
    status: str
    changed: bool
    summary: str
    before: int
    after: int
    items: int
    duration_ms: int
    detail: str
    failed: int = 0


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int


class FeedbackIn(BaseModel):
    dimension: str = EvalDimension.CORRECTNESS.value
    score: float
    source: str = "human"
    rater: str = ""
    comment: str = ""

    @field_validator("dimension")
    @classmethod
    def _valid_dimension(cls, value: str) -> str:
        """拒绝非法评测维度，避免脏值写入 DB（STATIC 约束之外的第一道校验）。"""
        if not EvalDimension.is_valid(value):
            raise ValueError(f"未知评测维度: {value}")
        return value


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


@router.get("/runs/{run_id}/input-stages", response_model=list[InputStageOut])
def get_input_stages(run_id: int) -> list[InputStageOut]:
    """一次运行的输入解析过程（全量，含 skip 的无改动阶段）。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        if repo.get_run(conn, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        stages = repo.list_input_stages(conn, run_id)
    return [_input_stage_out(s) for s in stages]


@router.get("/runs/{run_id}/health")
def get_run_health(run_id: int) -> dict[str, object]:
    """单次运行的健康分：总分 + 等级 + 置信度 + 四维明细（含扣分项）。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        run = repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        spans = repo.list_spans(conn, run_id)
        scores = repo.list_scores(conn, run_id)
    policy = _health_policy()
    report = health_mod.report_run(run, spans, scores, policy)
    model_calls = sum(1 for span in spans if span.type == "model")
    # 预算与实际耗时一起给：前端要画「花了多少 / 本该花多少」，不能只给一个分数
    return {
        "run_id": run_id,
        **report.as_dict(),
        "budget_ms": health_mod.latency_budget(model_calls, policy),
        "actual_ms": run.work_ms or run.duration_ms or 0,
        "model_calls": model_calls,
    }


@router.get("/metrics/health")
def metrics_health(limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, object]:
    """健康分分布与四维均值（按最近 N 次运行重算，不是缓存的旧分数）。

    为什么要重算：`runs.health_score` 是写入时算的，口径升级后与新算法不可比。
    看板只在有界窗口（默认 200 次）内重算，既能反映当前口径，又不会打爆生产库。
    """
    policy = _health_policy()
    with repo.connect() as conn:
        repo.init_db(conn)
        runs = repo.list_runs(conn, limit=limit)
        reports = [
            health_mod.report_run(
                run,
                repo.list_spans(conn, run.id) if run.id is not None else [],
                repo.list_scores(conn, run.id) if run.id is not None else [],
                policy,
            )
            for run in runs
        ]
    values = [report.score for report in reports]
    averages = health_mod.dimension_averages(reports)
    return {
        "version": health_mod.SCORING_VERSION,
        "runs": len(runs),
        "scored": len(values),
        "avg_score": health_mod.aggregate(values),
        "p50": health_mod.percentile(values, 0.5),
        "p90": health_mod.percentile(values, 0.9),
        "distribution": health_mod.distribution(values),
        "grades": health_mod.grade_counts(reports),
        "dimensions": averages,
        "deficits": health_mod.deficits(averages, policy),
        "weights": policy.weights.model_dump(),
        "coverage_avg": (
            round(sum(report.coverage for report in reports) / len(reports), 3) if reports else None
        ),
        "confidence_avg": (
            round(sum(report.confidence for report in reports) / len(reports), 3)
            if reports
            else None
        ),
    }


def _failure_clusters(runs: list[RunRecord], top: int = 8) -> list[dict[str, object]]:
    """把失败原因聚类：同一个报错反复出现才值得先修。"""
    counter: Counter[str] = Counter()
    for run in runs:
        if run.status == "success":
            continue
        reason = (run.error or "").strip().splitlines()
        key = reason[0][:120] if reason else "(未记录失败原因)"
        counter[key] += 1
    return [{"reason": text, "count": count} for text, count in counter.most_common(top)]


def _rate(part: int, whole: int) -> float | None:
    """安全比率：分母为 0 返回 None，别把「没有数据」说成 0%。"""
    return round(part / whole, 4) if whole else None


def _tool_block(stats: dict[str, int]) -> dict[str, object]:
    """工具调用块：次数、失败数、失败率。"""
    calls = stats.get("calls", 0)
    failed = stats.get("errors", 0)
    return {"calls": calls, "failed": failed, "failure_rate": _rate(failed, calls)}


def _permission_block(stats: dict[str, int]) -> dict[str, object]:
    """权限判定块：判定次数、拒绝数、拒绝率。"""
    decisions = stats.get("calls", 0)
    denied = stats.get("errors", 0)
    return {"decisions": decisions, "denied": denied, "deny_rate": _rate(denied, decisions)}


def _model_block(stats: dict[str, int]) -> dict[str, object]:
    """模型调用块：次数、token 合计、次均 token。"""
    calls = stats.get("calls", 0)
    tokens = stats.get("tokens", 0)
    return {
        "calls": calls,
        "tokens": tokens,
        "avg_tokens_per_call": round(tokens / calls) if calls else None,
    }


def _token_block(runs: list[RunRecord]) -> dict[str, object]:
    """token 块：输入/输出/缓存命中量与命中率。"""
    input_tokens = sum(run.input_tokens for run in runs)
    cache_tokens = sum(run.cache_input_tokens for run in runs)
    return {
        "input": input_tokens,
        "output": sum(run.output_tokens for run in runs),
        "cache_hit": cache_tokens,
        "cache_hit_rate": _rate(cache_tokens, input_tokens),
        # 老记录没有缓存字段（全是 0），前端据此显示「无缓存数据」而不是 0%
        "cache_recorded": any(
            run.cache_input_tokens or run.cache_creation_input_tokens for run in runs
        ),
    }


def _outcome_block(runs: list[RunRecord]) -> dict[str, int]:
    """流程结果：成功/失败/未完成各多少。"""
    counts = Counter(run.status for run in runs)
    succeeded = counts.get("success", 0)
    failed = counts.get("failed", 0)
    # 「未完成」= 既没成功也没失败（running / parked 等），不要只统计字面 running
    return {
        "total": len(runs),
        "success": succeeded,
        "failed": failed,
        "running": len(runs) - succeeded - failed,
    }


@router.get("/metrics/breakdown")
def metrics_breakdown() -> dict[str, object]:
    """全局指标拆解：工具/权限/缓存/流程结果，全部走 SQL 聚合不读明细。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        stats = repo.span_stats(conn)
        runs = repo.summary_rows(conn)
    return {
        "version": health_mod.SCORING_VERSION,
        "outcome": _outcome_block(runs),
        "tool": _tool_block(stats.get("tool", {})),
        "permission": _permission_block(stats.get("permission", {})),
        "model": _model_block(stats.get("model", {})),
        "tokens": _token_block(runs),
        "failures": _failure_clusters(runs),
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
        cache_input_tokens=run.cache_input_tokens,
        cache_creation_input_tokens=run.cache_creation_input_tokens,
        permission=run.permission,
        work_ms=run.work_ms,
        wall_ms=run.wall_ms,
        model_params=run.model_params,
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
        input_tokens=span.input_tokens,
        output_tokens=span.output_tokens,
        cache_input_tokens=span.cache_input_tokens,
        cache_creation_input_tokens=span.cache_creation_input_tokens,
        seq=span.seq,
        parent_id=span.parent_id,
    )


def _input_stage_out(stage: InputStageRecord) -> InputStageOut:
    """InputStageRecord → InputStageOut。"""
    return InputStageOut(
        seq=stage.seq,
        stage=stage.stage,
        label=stage.label,
        status=stage.status,
        changed=stage.changed,
        summary=stage.summary,
        before=stage.before,
        after=stage.after,
        items=stage.items,
        duration_ms=stage.duration_ms,
        detail=stage.detail,
        failed=stage.failed,
    )
