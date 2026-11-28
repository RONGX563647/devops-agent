"""指标与门禁：统计口径、空分母、红线优先（SPEC-0014 §6/§9）。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from devops_agent.openjudge.memory.gates import evaluate
from devops_agent.openjudge.memory.harness import CaseRun
from devops_agent.openjudge.memory.metrics import (  # noqa: I001
    aggregate_track,
    cost_of,
    gain_of,
    harm_of,
    mcnemar_exact,
    paired_bootstrap_ci,
    percentile,
    ratio,
)
from devops_agent.openjudge.memory.models import MemoryPlan
from devops_agent.openjudge.memory.suite import load_plan

_KEYS = (
    "probe_total",
    "probe_correct",
    "gold_total",
    "gold_retrieved",
    "gold_hits_total",
    "injected_total",
    "retrieval_n",
    "mrr_sum",
    "constraint_probe_total",
    "constraint_probe_failed",
    "fact_total",
    "fact_retained",
    "constraint_total",
    "constraint_retained",
    "artifact_total",
    "artifact_retained",
    "writes_total",
    "junk_writes",
    "junk_injected",
    "leak_count",
    "redaction_violation",
)


def _run(
    case_id: str,
    variant: str,
    track: str,
    passed: bool,
    *,
    trial: int = 0,
    memory_required: bool = True,
    tokens: int = 100,
    **metrics: int,
) -> CaseRun:
    run = CaseRun(
        case_id=case_id,
        track=track,
        tier="must_pass",
        variant=variant,
        trial=trial,
        memory_required=memory_required,
    )
    run.passed = passed
    run.rule_passed = passed
    run.metrics = {key: 0 for key in _KEYS}
    run.metrics.update(metrics)
    run.cost = {"input_tokens": tokens, "latency_ms": 10, "memory_bytes": 5}
    return run


def test_ratio_and_percentile_handle_empty() -> None:
    assert ratio(0, 0) is None
    assert ratio(1, 4) == 0.25
    assert percentile([], 0.5) is None
    assert percentile([1, 2, 3, 4], 0.5) == 2
    assert percentile([1, 2, 3, 4], 0.95) == 4


def test_mcnemar_and_bootstrap() -> None:
    assert mcnemar_exact(0, 0) is None
    assert mcnemar_exact(6, 0) is not None and mcnemar_exact(6, 0) < 0.05  # type: ignore[operator]
    assert paired_bootstrap_ci([(True, False)] * 6) == (1.0, 1.0)


def test_aggregate_track_uses_counts() -> None:
    runs = [
        _run(
            "c1",
            "full",
            "RT",
            True,
            gold_total=2,
            gold_retrieved=2,
            gold_hits_total=1,
            injected_total=1,
            retrieval_n=1,
            mrr_sum=1.0,
            probe_total=1,
            probe_correct=1,
        ),
        _run(
            "c2",
            "full",
            "RT",
            False,
            gold_total=2,
            gold_retrieved=1,
            gold_hits_total=1,
            injected_total=2,
            retrieval_n=1,
            mrr_sum=0.5,
            probe_total=1,
            probe_correct=0,
        ),
    ]
    row = aggregate_track(runs)
    assert row["total"] == 2 and row["passed"] == 1
    assert row["recall_at_5"] == 0.75
    assert row["precision_at_5"] == 2 / 3
    assert row["mrr"] == 0.75
    assert row["ci"] is not None


def test_gain_and_harm_are_paired() -> None:
    primary = [_run(f"c{index}", "full", "MT", True) for index in range(4)]
    baseline = [_run(f"c{index}", "off", "MT", False) for index in range(4)]
    gain = gain_of(primary, baseline)
    assert gain["n"] == 4
    assert gain["gain_pp"] == 100.0
    assert gain["p"] is not None
    harm = harm_of(
        [_run("n1", "full", "NB", True, memory_required=False)],
        [_run("n1", "off", "NB", True, memory_required=False)],
    )
    assert harm["harm_rate"] == 0.0


def test_cost_overhead_pairs_tokens() -> None:
    primary = [_run("c1", "full", "MT", True, tokens=150)]
    baseline = [_run("c1", "off", "MT", True, tokens=100)]
    cost = cost_of(primary, baseline)
    assert cost["token_overhead_p50"] == 0.5
    assert cost["latency_p95_ms"] == 10


def _plan() -> MemoryPlan:
    return load_plan(Path("eval/memory/plan.yaml"))


def test_gate_red_line_forces_fail() -> None:
    plan = _plan()
    tracks = {"PR": {"total": 4, "passed": 4, "leak_count": 1, "redaction_violation": 0}}
    decision = evaluate(plan, tracks, {}, {}, 0)
    assert decision["verdict"] == "fail"
    assert any(item["code"] == "leak_above" for item in decision["causes"])


def test_gate_sample_short_is_insufficient() -> None:
    plan = replace(_plan(), min_n=10)
    tracks = {"PR": {"total": 4, "passed": 4, "leak_count": 0, "redaction_violation": 0}}
    decision = evaluate(plan, tracks, {}, {}, 0)
    assert decision["verdict"] == "insufficient"


def test_gate_below_threshold_fails() -> None:
    plan = _plan()
    tracks = {
        "MT": {
            "total": 12,
            "passed": 6,
            "probe_acc": 0.5,
            "contradiction_rate": 0.0,
            "leak_count": 0,
            "redaction_violation": 0,
        }
    }
    gains = {"MT": {"n": 12, "gain_pp": 100.0, "p": 0.001, "ci": (0.5, 1.0)}}
    decision = evaluate(plan, tracks, gains, {}, 0)
    assert decision["verdict"] == "fail"


def test_gate_all_pass() -> None:
    plan = _plan()
    tracks = {
        "MT": {
            "total": 12,
            "passed": 12,
            "probe_acc": 1.0,
            "contradiction_rate": 0.0,
            "leak_count": 0,
            "redaction_violation": 0,
        }
    }
    gains = {"MT": {"n": 12, "gain_pp": 100.0, "p": 0.001, "ci": (0.5, 1.0)}}
    decision = evaluate(plan, tracks, gains, {}, 0)
    assert decision["verdict"] == "pass"
