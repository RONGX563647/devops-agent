"""批次汇总：把 CaseRun 组织成前端契约（SPEC §11.2/§11.3）。

结论、逐赛道指标、增益、红线、代价、失败清单一次算清；报告层不再二次加工。
"""

from __future__ import annotations

from typing import Any

from devops_agent.openjudge.memory.gates import evaluate
from devops_agent.openjudge.memory.harness import CaseRun
from devops_agent.openjudge.memory.metrics import (
    aggregate_track,
    cost_of,
    gain_of,
    harm_of,
)
from devops_agent.openjudge.memory.models import TRACKS, MemoryPlan

MAX_FAILURES = 50


def _subset(runs: list[CaseRun], *, variant: str, track: str | None = None) -> list[CaseRun]:
    return [run for run in runs if run.variant == variant and (track is None or run.track == track)]


def _red_lines(tracks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    constraint = [
        float(row["constraint_retention"])
        for row in tracks.values()
        if row.get("constraint_retention") is not None
    ]
    return {
        "leak_count": sum(int(row.get("leak_count", 0)) for row in tracks.values()),
        "redaction_violation": sum(
            int(row.get("redaction_violation", 0)) for row in tracks.values()
        ),
        "constraint_retention": min(constraint) if constraint else None,
    }


def _failures(runs: list[CaseRun], variant: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for run in runs:
        if run.variant != variant or run.passed:
            continue
        failed = [probe for probe in run.probes if not probe.correct]
        failures.append(
            {
                "case_id": run.case_id,
                "track": run.track,
                "variant": run.variant,
                "trial": run.trial,
                "error": run.error,
                "failed_probes": [
                    {"question": probe.question, "kind": probe.kind, "answer": probe.answer}
                    for probe in failed
                ],
            }
        )
    return failures[:MAX_FAILURES]


def _tracks_and_gains(
    runs: list[CaseRun], plan: MemoryPlan
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    tracks: dict[str, dict[str, Any]] = {}
    gains: dict[str, dict[str, Any]] = {}
    for track in TRACKS:
        primary = _subset(runs, variant=plan.primary, track=track)
        if not primary:
            continue
        tracks[track] = aggregate_track(primary)
        gains[track] = gain_of(primary, _subset(runs, variant=plan.baseline, track=track))
    return tracks, gains


def _kpi(runs: list[CaseRun], passed: int) -> dict[str, Any]:
    return {
        "runs": len(runs),
        "cases": len({(run.case_id, run.variant) for run in runs}),
        "passed": passed,
        "pass_rate": passed / len(runs) if runs else None,
    }


def summarize(
    runs: list[CaseRun],
    plan: MemoryPlan,
    *,
    suite_sha: str,
    plan_sha: str,
    config_shas: dict[str, str] | None = None,
) -> dict[str, Any]:
    """生成批次 summary_json；结构即 API 契约。"""
    must_pass = [run for run in runs if run.tier == "must_pass"]
    regression = [run for run in runs if run.tier != "must_pass"]
    tracks, gains = _tracks_and_gains(must_pass, plan)
    harm = harm_of(
        _subset(must_pass, variant=plan.primary),
        _subset(must_pass, variant=plan.baseline),
    )
    cost = cost_of(
        _subset(must_pass, variant=plan.primary),
        _subset(must_pass, variant=plan.baseline),
    )
    regression_failed = sum(1 for run in regression if not run.passed)
    decision = evaluate(plan, tracks, gains, harm, regression_failed)
    passed = sum(1 for run in runs if run.passed)
    return {
        "plan": plan.name,
        "baseline": plan.baseline,
        "primary": plan.primary,
        "variants": config_shas or {},
        "suite_sha": suite_sha,
        "plan_sha": plan_sha,
        "verdict": decision["verdict"],
        "causes": decision["causes"],
        "track_verdicts": decision["tracks"],
        "kpi": _kpi(runs, passed),
        "tracks": tracks,
        "gains": gains,
        "harm": harm,
        "red_lines": _red_lines(tracks),
        "cost": cost,
        "regression_failed": regression_failed,
        "failures": _failures(runs, plan.primary),
    }
