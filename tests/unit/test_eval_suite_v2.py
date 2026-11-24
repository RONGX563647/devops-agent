"""devops-core.v2 题库的结构与配比单测。

这些断言把「题库可信」这件事钉住：配比、覆盖、判分方向。数值不一致时报错，
而不是靠人眼看 JSONL。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from devops_agent.eval.dataset import (
    ALLOWED_JUDGE_DIMENSIONS,
    RATIO_TARGETS,
    RATIO_TOLERANCE,
    SUBJECTS,
    load_suite,
)
from devops_agent.eval.lint import assertion_count, run_lint, split_findings

SUITE_PATH = Path("eval/suites/devops-core.v2.jsonl")
FIXTURES = Path("eval/fixtures")

suite = load_suite(SUITE_PATH)
CASES = suite.cases


def scenario_of(case) -> str:  # noqa: ANN001 - 测试辅助
    return next(t for t in case.tags if t.startswith("scenario:")).split(":")[1]


# ------------------------------------------------------------------ 基础规模


def test_case_count_matches_gate() -> None:
    """题库条数必须达到 min_cases_for_gate，否则永远 insufficient。"""
    assert len(CASES) == 40
    assert suite.manifest.min_cases_for_gate == 40
    assert len(CASES) >= suite.manifest.min_cases_for_gate


def test_ids_unique() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_all_cases_have_scenario_tag() -> None:
    for case in CASES:
        assert scenario_of(case) in RATIO_TARGETS, case.id


# ------------------------------------------------------------------ 配比


@pytest.mark.parametrize(("scenario", "bounds"), sorted(RATIO_TARGETS.items()))
def test_scenario_ratio_within_tolerance(scenario: str, bounds: tuple[float, float]) -> None:
    counts = Counter(scenario_of(case) for case in CASES)
    share = counts[scenario] / len(CASES)
    low, high = bounds
    assert low - RATIO_TOLERANCE <= share <= high + RATIO_TOLERANCE, (
        f"{scenario} 占比 {share:.1%} 超出 [{low:.0%}, {high:.0%}]±{RATIO_TOLERANCE:.0%}"
    )


def test_manifest_counts_match_actual() -> None:
    counts = Counter(scenario_of(case) for case in CASES)
    assert dict(sorted(counts.items())) == suite.manifest.counts


# ------------------------------------------------------------------ 覆盖


def test_every_subject_declared_value_used_or_explained() -> None:
    for case in CASES:
        assert case.subject in SUBJECTS, case.id
    used = {case.subject for case in CASES}
    # 五类里至少用到四类；没用到的那类要能说出理由
    assert len(used) >= 4, used


def test_every_fixture_exists() -> None:
    for case in CASES:
        if case.fixture:
            assert (FIXTURES / case.fixture).is_dir(), case.id


def test_permissions_are_known_profiles() -> None:
    from devops_agent.governance.profiles import PROFILES

    for case in CASES:
        assert case.permission in PROFILES, case.id


def test_multiple_permissions_exercised() -> None:
    """题库必须真的测到不同档位，否则「档位行为」类结论无从谈起。"""
    used = {case.permission for case in CASES}
    assert {"readonly", "execute", "workspace", "auto"} <= used


# ------------------------------------------------------------------ judge


def test_judge_dimensions_fully_covered() -> None:
    """六个维度都要有用例请求——只声明不使用的维度是摆设。"""
    dims = Counter()
    for case in CASES:
        if case.judge:
            for dimension in case.judge.dimensions:
                dims[dimension] += 1
    assert set(dims) == ALLOWED_JUDGE_DIMENSIONS, sorted(ALLOWED_JUDGE_DIMENSIONS - set(dims))


def test_judge_correctness_meets_min_samples() -> None:
    """correctness 样本数必须达到门禁的 min_samples，否则门禁永远 insufficient。"""
    judged = [c for c in CASES if c.judge and "correctness" in c.judge.dimensions]
    threshold = suite.manifest.thresholds["judge_correctness"]
    assert len(judged) >= threshold.min_samples, (len(judged), threshold.min_samples)


def test_every_judge_dimension_declared_is_allowed() -> None:
    for case in CASES:
        if case.judge:
            assert set(case.judge.dimensions) <= ALLOWED_JUDGE_DIMENSIONS, case.id


# ------------------------------------------------------------------ 断言强度


def test_lint_is_clean() -> None:
    """新题库在全部静态规则下必须 0 error（不含豁免清单）。"""
    errors, _warnings = split_findings(run_lint(suite, FIXTURES))
    assert errors == [], [(e.rule, e.where, e.message) for e in errors]


def test_no_dead_assertions() -> None:
    """恒真断言（该档根本不装配的工具）不应存在。"""
    from devops_agent.eval.lint import _dead_assertions

    offenders = {case.id: _dead_assertions(case) for case in CASES if _dead_assertions(case)}
    assert offenders == {}


def test_assertion_count_reasonable() -> None:
    """每条用例至少两条断言：一条太容易被单点满足。"""
    for case in CASES:
        assert assertion_count(case) >= 2, (case.id, assertion_count(case))


def test_cases_with_bash_have_command_hint() -> None:
    """断言了 Bash 却没说清「要跑什么」-> S8 无法预演。"""
    for case in CASES:
        if case.expected.tool_calls and "Bash" in case.expected.tool_calls.must:
            assert case.command_hint, case.id


def test_pending_cases_do_not_require_execution() -> None:
    """pending 用例走确认流，工具本来就不会执行。"""
    for case in CASES:
        if case.expected.status == "pending":
            assert case.require_executed is False, case.id


def test_must_cite_targets_exist_in_fixture() -> None:
    """must_cite 的路径必须在该用例的 fixture 里真实存在。"""
    for case in CASES:
        if not case.expected.must_cite:
            continue
        root = FIXTURES / (case.fixture or "")
        for path in case.expected.must_cite:
            assert (root / path).exists(), f"{case.id}: {path} 不在 {root}"


# ------------------------------------------------------------------ manifest


def test_manifest_thresholds_declare_min_samples() -> None:
    """judge / trajectory 的 min_samples 必须 > 0，否则 3 条样本就能判门禁。"""
    thresholds = suite.manifest.thresholds
    assert thresholds["judge_correctness"].min_samples >= 10
    assert thresholds["trajectory"].min_samples >= 10


def test_manifest_sha_matches_cases() -> None:
    from devops_agent.eval.dataset import suite_sha256

    assert suite.manifest.sha256 == suite_sha256(CASES)


def test_manifest_version_bumped() -> None:
    """v1 -> v2 是破坏性变更（题库与字段都变了），版本必须升。"""
    assert suite.manifest.version == "2.0.0"
    manifest = json.loads(
        Path("eval/suites/devops-core.v2.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["suite"] == "devops-core"
    assert manifest["version"] == "2.0.0"
