"""健康分计算单测（纯函数，不碰 DB）。

覆盖口径 v2 的每条规则：扣分上限、按步数给的预算、幂律衰减、先验收缩、
缺证据时的置信度，以及兼容层仍返回一个数字。
"""

from devops_agent.config import HealthPolicy, HealthWeights
from devops_agent.observability.health import (
    SCORING_VERSION,
    aggregate,
    deficits,
    dimension_averages,
    distribution,
    efficiency_detail,
    execution_detail,
    grade_counts,
    grade_for,
    percentile,
    quality_detail,
    report_run,
    safety_detail,
    score_run,
    summarize,
)
from devops_agent.observability.models import EvalScoreRecord, RunRecord, SpanRecord

_POLICY = HealthPolicy()


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


def _score(dimension: str, value: float, source: str = "human") -> EvalScoreRecord:
    return EvalScoreRecord(
        id=1,
        run_id=1,
        dimension=dimension,
        score=value,
        source=source,
        rater="u",
        created_at="2026-11-23T14:00:00+00:00",
        comment="",
    )


def test_execution_state_and_tool_failures() -> None:
    assert execution_detail(_run(), []).value == 100.0
    assert execution_detail(_run(status="failed"), []).value == 0.0
    # 运行中不评分：没跑完的分数没有意义
    assert execution_detail(_run(status="running"), []).value is None
    # 1/5 失败：扣 8 分，失败率没超过 20% 阈值
    tools = [_span(type="tool", status="error")] + [_span(type="tool") for _ in range(4)]
    assert execution_detail(_run(), tools).value == 92.0
    # 1/1 失败：扣 8 分，再按 100% 失败率扣满 18 分
    assert execution_detail(_run(), [_span(type="tool", status="error")]).value == 74.0
    # 5 个失败：单次扣分封顶 24，失败率再扣 18
    all_failed = [_span(type="tool", status="error") for _ in range(5)]
    assert execution_detail(_run(), all_failed).value == 58.0


def test_execution_penalizes_unconverged_run() -> None:
    run = _run(error="Agent devops exceeds the max iteration numbers 12. Stop the react loop.")
    detail = execution_detail(run, [])
    assert detail.value == 85.0
    assert any(item.label == "未收敛" for item in detail.items)


def test_execution_failed_run_does_not_double_penalize() -> None:
    detail = execution_detail(_run(status="failed"), [_span(type="tool", status="error")])
    assert detail.value == 0.0


def test_efficiency_respects_budget_and_power_decay() -> None:
    # 单步：预算 5000ms
    assert efficiency_detail(_run(duration_ms=2000), [], _POLICY).value == 100.0
    # 5 次模型调用：预算 5000 + 4×6000 = 29000ms，正好用满仍是满分
    spans = [_span() for _ in range(5)]
    assert efficiency_detail(_run(duration_ms=29000), spans, _POLICY).value == 100.0
    # 用掉两倍预算：100 × 0.5^0.6 ≈ 66，不再一刀切归零
    assert efficiency_detail(_run(duration_ms=58000), spans, _POLICY).value == 66.0
    # 缺耗时不白拿满分，只保留 token 项
    no_duration = efficiency_detail(
        _run(duration_ms=None, work_ms=0, input_tokens=60, output_tokens=40), [_span()], _POLICY
    )
    assert no_duration.value == 100.0
    # 只有 token 一项证据 → 置信度就是它在配置里的权重占比
    assert no_duration.confidence == 0.3


def test_efficiency_uses_work_ms_before_duration() -> None:
    # work_ms 是真正干活的时间，优先于含空闲的 duration_ms
    run = _run(duration_ms=99000, work_ms=3000)
    assert efficiency_detail(run, [], _POLICY).value == 100.0


def test_efficiency_token_intensity() -> None:
    # 单次 24000 token，是 12000 预算的 2 倍：token 项 70.7，只占 30%
    heavy = _run(duration_ms=1000, input_tokens=20000, output_tokens=4000)
    detail = efficiency_detail(heavy, [_span()], _POLICY)
    expected = round(0.7 * 100.0 + 0.3 * 100.0 * (0.5**0.5), 1)
    assert detail.value == expected
    assert detail.value is not None and detail.value < 100.0


def test_efficiency_without_any_evidence_is_none() -> None:
    detail = efficiency_detail(_run(duration_ms=None, work_ms=0), [], _POLICY)
    assert detail.value is None
    assert detail.confidence == 0.0


def test_quality_shrinks_toward_prior() -> None:
    assert quality_detail([], _POLICY).value is None
    # 忽略非 correctness 维度与越界值
    assert quality_detail([_score("safety", 0.9)], _POLICY).value is None
    assert quality_detail([_score("correctness", 2.0)], _POLICY).value is None
    # 单条 0 分被先验抬到 0.25，而不是直接判死
    assert quality_detail([_score("correctness", 0.0)], _POLICY).value == 25.0
    assert quality_detail([_score("correctness", 1.0)], _POLICY).value == 75.0


def test_quality_weights_sources() -> None:
    # human 0.8（权重 1.0）+ judge 0.6（权重 0.8）
    detail = quality_detail(
        [_score("correctness", 0.8, "human"), _score("correctness", 0.6, "judge")], _POLICY
    )
    raw = (0.8 * 1.0 + 0.6 * 0.8) / 1.8
    expected = round((raw * 1.8 + 0.5 * 1.0) / 2.8 * 100.0, 1)
    assert detail.value == expected
    # 样本越多，置信度越高
    assert 0.0 < detail.confidence < 1.0


def test_safety_denials_and_missing_evidence() -> None:
    assert safety_detail([]).value == 100.0
    # 没有权限判定不等于安全：满分但置信度低
    assert safety_detail([]).confidence == 0.4
    assert safety_detail([_span(type="permission")]).value == 100.0
    denied = _span(type="permission", status="error")
    ok = _span(type="permission", status="ok")
    assert safety_detail([denied, ok]).value == 70.0
    # 单次全拒：60 分；重复越界再扣
    assert safety_detail([denied]).value == 40.0
    assert safety_detail([denied, denied, denied]).value == 30.0


def test_report_renormalizes_missing_dimensions() -> None:
    report = report_run(_run(), [_span()], [], _POLICY)
    assert report.version == SCORING_VERSION
    # quality 缺失 → 权重和 0.75，其余三项满分
    assert report.score == 100.0
    assert report.coverage == 0.75
    assert report.grade == "A"
    assert report.dimensions["quality"].value is None
    # 置信度按维度权重加权：efficiency 只有耗时证据（0.7），safety 无判定记录（0.4）
    assert 0.7 < report.confidence < 0.85


def test_report_failed_run_is_low_and_grade_f() -> None:
    report = report_run(_run(status="failed", duration_ms=0, work_ms=0), [], [], _POLICY)
    assert report.grade == "F"
    assert report.score < 40.0
    # 只剩执行与安全两维，覆盖度 0.5
    assert report.coverage == 0.5


def test_report_as_dict_is_json_shaped() -> None:
    payload = report_run(_run(), [_span()], [], _POLICY).as_dict()
    assert payload["score"] == 100.0
    dimensions = payload["dimensions"]
    assert isinstance(dimensions, dict)
    execution = dimensions["execution"]
    assert isinstance(execution, dict)
    assert execution["label"] == "执行健康"
    assert isinstance(execution["items"], list)


def test_grade_thresholds() -> None:
    assert grade_for(95.0, _POLICY) == "A"
    assert grade_for(85.0, _POLICY) == "B"
    assert grade_for(75.0, _POLICY) == "C"
    assert grade_for(65.0, _POLICY) == "D"
    assert grade_for(10.0, _POLICY) == "F"


def test_score_run_all_zero_weights() -> None:
    zero_policy = HealthPolicy(
        weights=HealthWeights(execution=0, efficiency=0, quality=0, safety=0)
    )
    assert score_run(_run(), [], [], zero_policy) == 0.0


def test_distribution_buckets() -> None:
    buckets = distribution([0.0, 5.0, 55.0, 100.0])
    assert len(buckets) == 10
    assert buckets[0]["count"] == 2
    assert buckets[5]["count"] == 1
    # 100 分归入最后一桶而不是溢出
    assert buckets[9]["count"] == 1
    assert buckets[9]["bucket"] == "90-100"


def test_dimension_averages_and_deficits() -> None:
    reports = [report_run(_run(), [_span()], [], _POLICY) for _ in range(2)]
    averages = dimension_averages(reports)
    assert averages["execution"]["avg"] == 100.0
    # quality 没有样本 → 均值 None，且不参与拖分排行
    assert averages["quality"]["avg"] is None
    rows = deficits(averages, _POLICY)
    assert all(row["key"] != "quality" for row in rows)
    assert all(float(row["loss"]) == 0.0 for row in rows)


def test_grade_counts() -> None:
    good = report_run(_run(), [_span()], [], _POLICY)
    bad = report_run(_run(status="failed", duration_ms=0, work_ms=0), [], [], _POLICY)
    assert grade_counts([good, good, bad]) == {"A": 2, "F": 1}


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
