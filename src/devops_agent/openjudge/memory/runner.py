"""批次编排：题库 × 变体 × 试验，落库并产出 summary（SPEC §8.2）。

每个 (case, variant, trial) 用临时目录里的独立 SQLite，跑完即弃：
case 之间不共享记忆，顺序依赖不可能成立。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devops_agent.openjudge import store as oj_store
from devops_agent.openjudge.memory import store
from devops_agent.openjudge.memory.harness import CaseRun, run_case
from devops_agent.openjudge.memory.models import MemoryCase, MemoryPlan, VariantSpec
from devops_agent.openjudge.memory.report import summarize
from devops_agent.openjudge.memory.suite import (
    load_cases,
    load_plan,
    plan_sha,
    suite_sha,
    validate,
)
from devops_agent.openjudge.memory.variants import config_sha

_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PLAN = _REPO_ROOT / "eval" / "memory" / "plan.yaml"


class BatchError(ValueError):
    """题库/计划不合法，不能开跑。"""


@dataclass(slots=True)
class BatchResult:
    batch_id: int
    verdict: str
    summary: dict[str, Any]
    runs: list[CaseRun] = field(default_factory=list)


def _selected(plan_variants: dict[str, VariantSpec], baseline: str, primary: str) -> list[str]:
    order = list(plan_variants)
    for name in (baseline, primary):
        if name not in order:
            order.append(name)
    return order


def _prepare(
    target: Path, suite_tier: str, variants: list[str] | None
) -> tuple[MemoryPlan, list[MemoryCase], list[str], dict[str, VariantSpec], dict[str, str]]:
    plan = load_plan(target)
    cases = load_cases(plan.suite_dir)
    problems = validate(cases)
    if problems:
        raise BatchError("；".join(problems))
    cases = [case for case in cases if case.tier == suite_tier] if suite_tier else cases
    if not cases:
        raise BatchError(f"没有 tier={suite_tier} 的用例")
    selected = variants or _selected(plan.variants, plan.baseline, plan.primary)
    specs = {name: plan.variants.get(name) or VariantSpec(name) for name in selected}
    shas = {name: config_sha(name, spec.overrides) for name, spec in specs.items()}
    return plan, cases, selected, specs, shas


async def _execute(selected: list[str], cases: list[MemoryCase], trials: int) -> list[CaseRun]:
    runs: list[CaseRun] = []
    with tempfile.TemporaryDirectory(prefix="memory-eval-") as tmp:
        tmp_dir = Path(tmp)
        for variant in selected:
            for case in cases:
                for trial in range(trials):
                    runs.append(await run_case(case, variant, trial, tmp_dir))
    return runs


async def run_plan(
    plan_path: Path | None = None,
    *,
    db_path: Path | None = None,
    suite_tier: str = "must_pass",
    variants: list[str] | None = None,
) -> BatchResult:
    """执行一次记忆评测批次；返回落库 id 与 summary。"""
    target = plan_path or DEFAULT_PLAN
    plan, cases, selected, specs, shas = _prepare(target, suite_tier, variants)
    started_at = oj_store.now_iso()
    runs = await _execute(selected, cases, plan.trials)
    summary = summarize(
        runs,
        plan,
        suite_sha=suite_sha(cases),
        plan_sha=plan_sha(target),
        config_shas=shas,
    )
    batch_id = _persist(
        plan_name=plan.name,
        suite_hash=suite_sha(cases),
        plan_hash=plan_sha(target),
        config_hash=shas[plan.primary] if plan.primary in shas else "",
        specs=specs,
        shas=shas,
        runs=runs,
        summary=summary,
        started_at=started_at,
        db_path=db_path,
    )
    return BatchResult(
        batch_id=batch_id,
        verdict=str(summary["verdict"]),
        summary=summary,
        runs=runs,
    )


def _persist(
    *,
    plan_name: str,
    suite_hash: str,
    plan_hash: str,
    config_hash: str,
    specs: dict[str, VariantSpec],
    shas: dict[str, str],
    runs: list[CaseRun],
    summary: dict[str, Any],
    started_at: str,
    db_path: Path | None,
) -> int:
    with oj_store.connect(db_path) as conn:
        oj_store.init_db(conn)
        store.init_db(conn)
        batch_id = store.insert_batch(
            conn,
            plan_name=plan_name,
            suite_sha=suite_hash,
            plan_sha=plan_hash,
            config_sha=config_hash,
            model="deterministic",
            oj_version="1",
            started_at=started_at,
            finished_at=oj_store.now_iso(),
            verdict=str(summary["verdict"]),
            summary=summary,
        )
        for name, spec in specs.items():
            store.insert_variant(
                conn, batch_id, name=name, overrides=spec.overrides, config_sha=shas[name]
            )
        for run in runs:
            row_id = store.insert_case(conn, batch_id, run)
            for probe in run.probes:
                store.insert_probe(conn, row_id, probe)
        conn.commit()
    return batch_id
