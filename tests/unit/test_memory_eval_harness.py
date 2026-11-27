"""确定性执行器：变体差异、会话隔离与隐私脱敏（SPEC-0014 §6/§10）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from devops_agent.openjudge.memory.harness import run_case
from devops_agent.openjudge.memory.suite import load_cases, load_plan
from devops_agent.openjudge.memory.variants import EVAL_MIN_SCORE


def _cases() -> dict[str, object]:
    plan = load_plan(Path("eval/memory/plan.yaml"))
    return {case.id: case for case in load_cases(plan.suite_dir)}


def test_off_variant_has_no_memory_and_full_recalls(tmp_path: Path) -> None:
    case = _cases()["mt-001"]
    off = asyncio.run(run_case(case, "off", 0, tmp_path))
    full = asyncio.run(run_case(case, "full", 0, tmp_path))
    assert off.passed is False
    assert off.probes[0].answer == ""
    assert full.passed is True
    assert "周四" in full.probes[0].answer


def test_cross_session_memory_is_visible(tmp_path: Path) -> None:
    case = _cases()["ms-001"]
    full = asyncio.run(run_case(case, "full", 0, tmp_path))
    assert full.passed is True
    assert full.probes[0].correct is True


def test_compression_retains_facts_constraints_and_artifacts(tmp_path: Path) -> None:
    case = _cases()["cp-001"]
    full = asyncio.run(run_case(case, "full", 0, tmp_path))
    assert full.passed is True
    assert full.metrics["fact_retained"] == full.metrics["fact_total"]
    assert full.metrics["constraint_retained"] == full.metrics["constraint_total"]
    assert full.metrics["artifact_retained"] == full.metrics["artifact_total"]


def test_privacy_secret_is_redacted_and_not_leaked(tmp_path: Path) -> None:
    case = _cases()["pr-001"]
    secret = case.privacy[0]
    full = asyncio.run(run_case(case, "full", 0, tmp_path))
    assert full.passed is True
    assert full.metrics["redaction_violation"] == 0
    assert full.metrics["leak_count"] == 0
    assert secret not in json.dumps(full.trace, ensure_ascii=False)
    assert all(secret not in probe.answer for probe in full.probes)


def test_case_uses_isolated_db(tmp_path: Path) -> None:
    cases = _cases()
    first = asyncio.run(run_case(cases["mt-001"], "full", 0, tmp_path))
    second = asyncio.run(run_case(cases["mt-002"], "full", 0, tmp_path))
    assert first.passed and second.passed
    # 两条用例的事实不同：第一条的答案里不应该出现第二条的事实。
    assert "db.internal" not in first.probes[0].answer
    assert "周四" not in second.probes[0].answer


def test_eval_min_score_filters_distractors(tmp_path: Path) -> None:
    case = _cases()["rt-001"]
    full = asyncio.run(run_case(case, "full", 0, tmp_path))
    assert EVAL_MIN_SCORE >= 0.7
    assert full.metrics["gold_hits_total"] == full.metrics["injected_total"]
    assert full.metrics["gold_retrieved"] == full.metrics["gold_total"]
