"""健康分计算单测（纯函数，不碰 DB）。"""

from devops_agent.config import HealthPolicy, HealthWeights
from devops_agent.observability.health import (
    aggregate,
    efficiency_score,
    execution_score,
    percentile,
    quality_score,
    safety_score,
    score_run,
    summarize,
)
from devops_agent.observability.models import EvalScoreRecord, RunRecord, SpanRecord

_POLICY = HealthPolicy(weights=HealthWeights(), latency_baseline_ms=5000)


def _run(**kwargs: object) -> RunRecord:
    defaults: dict[str, object] = dict(
        id=1,
        session_id="s1",
        domain="debug",
        status="success",
        input="in",
        output="out",
        started_at="2026-11-23T14:00:00+00:00",
        ended_at="2026-11-23T14:00:02+00:00",
        duration_ms=2000,
        model="m",
        total_tokens=100,
        health_score=None,
    )
    defaults.update(kwargs)
    return RunRecord(**defaults)  # type: ignore[arg-type]


def _span(**kwargs: object) -> SpanRecord:
    defaults: dict[str, object] = dict(
        id=1,
        run_id=1,
        type="model",
        name="m",
        start_offset_ms=0,
        duration_ms=100,
        input="",
        output="",
        tokens=100,
        status="ok",
        error=None,
    )
    defaults.update(kwargs)
    return SpanRecord(**defaults)  # type: ignore[arg-type]


def _score(dimension: str, value: float) -> EvalScoreRecord:
    return EvalScoreRecord(
        id=1,
        run_id=1,
        dimension=dimension,
        score=value,
        source="human",
        rater="u",
        created_at="2026-11-23T14:00:00+00:00",
        comment="",
    )


def test_execution_score_success_and_failure() -> None:
    assert execution_score(_run(), []) == 100.0
    assert execution_score(_run(status="failed"), []) == 0.0
    failed_span = _span(status="error")
    assert execution_score(_run(), [failed_span]) == 90.0
    # 失败 run 不重复扣分
    assert execution_score(_run(status="failed"), [failed_span]) == 0.0


def test_efficiency_score_baseline_and_decay() -> None:
    assert efficiency_score(_run(duration_ms=1000), 5000) == 100.0
    assert efficiency_score(_run(duration_ms=None), 5000) == 100.0
    assert efficiency_score(_run(duration_ms=7500), 5000) == 50.0
    assert efficiency_score(_run(duration_ms=15000), 5000) == 0.0


def test_quality_score_ignores_out_of_range_and_missing() -> None:
    assert quality_score([]) is None
    assert quality_score([_score("safety", 0.9)]) is None
    assert quality_score([_score("correctness", 2.0)]) is None
    assert quality_score([_score("correctness", 0.8), _score("correctness", 0.6)]) == 70.0


def test_safety_score_with_permission_spans() -> None:
    assert safety_score([]) == 100.0
    assert safety_score([_span(type="permission")]) == 100.0
    denied = _span(type="permission", status="error")
    ok = _span(type="permission", status="ok")
    assert safety_score([denied, ok]) == 50.0


def test_score_run_renormalizes_missing_quality() -> None:
    run = _run()
    spans = [_span()]
    # quality 缺失 → 剔除后权重重归一化：(0.35*100 + 0.25*100 + 0.15*100)/0.75
    result = score_run(run, spans, [], _POLICY)
    assert 99.0 <= result <= 100.0
    # 带 correctness 评分时也落在合法区间
    with_quality = score_run(run, spans, [_score("correctness", 0.9)], _POLICY)
    assert 0.0 <= with_quality <= 100.0


def test_score_run_all_zero_weights() -> None:
    zero_policy = HealthPolicy(
        weights=HealthWeights(execution=0, efficiency=0, quality=0, safety=0)
    )
    assert score_run(_run(), [], [], zero_policy) == 0.0


def test_percentile_and_aggregate() -> None:
    assert percentile([], 0.95) is None
    assert percentile([10, 20, 30, 40], 0.95) == 40
    assert aggregate([]) is None
    assert aggregate([80.0, 60.0]) == 70.0


def test_summarize_empty_and_populated() -> None:
    empty = summarize([])
    assert empty["total_runs"] == 0
    assert empty["success_rate"] is None
    populated = summarize(
        [
            _run(
                status="success",
                duration_ms=1000,
                input_tokens=10,
                output_tokens=5,
                health_score=80.0,
            ),
            _run(
                status="failed",
                duration_ms=3000,
                input_tokens=20,
                output_tokens=10,
                health_score=None,
            ),
        ]
    )
    assert populated["total_runs"] == 2
    assert populated["success_count"] == 1
    assert populated["failed_count"] == 1
    assert populated["success_rate"] == 0.5
    assert populated["avg_health_score"] == 80.0
    assert populated["p95_duration_ms"] == 3000
    assert populated["total_input_tokens"] == 30
    assert populated["total_output_tokens"] == 15
    assert populated["token_proxy"] == 45


def test_summarize_reports_prefix_cache_hit_rate() -> None:
    """缓存命中率 = 命中读 / 输入；没有输入时返回 None 而不是除零。"""
    empty = summarize([_run(input_tokens=0, cache_input_tokens=50)])
    assert empty["cache_read_tokens"] == 50
    assert empty["cache_hit_rate"] is None

    populated = summarize(
        [
            _run(input_tokens=100, cache_input_tokens=60, cache_creation_input_tokens=10),
            _run(input_tokens=200, cache_input_tokens=60, cache_creation_input_tokens=20),
        ]
    )
    assert populated["cache_read_tokens"] == 120
    assert populated["cache_write_tokens"] == 30
    assert populated["cache_hit_rate"] == 0.4
