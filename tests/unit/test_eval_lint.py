"""静态契约检查单测：恒真断言、清单版本、资产自洽与断言计数。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devops_agent.eval.dataset import GRADER_VERSION, EvalCase, Manifest, Suite, suite_sha256
from devops_agent.eval.lint import (
    assertion_summary,
    run_lint,
    split_findings,
)


def make_suite(case: EvalCase, *, grader: str = GRADER_VERSION) -> Suite:
    manifest = Manifest(
        suite="demo",
        version="1",
        sha256=suite_sha256([case]),
        counts={"common": 1},
        min_cases_for_gate=1,
        grader_version=grader,
    )
    return Suite(manifest=manifest, cases=[case])


def make_case(expected: dict[str, Any], **overrides: Any) -> EvalCase:
    data: dict[str, Any] = {
        "id": "c-1",
        "tags": ["scenario:common"],
        "input": "看看 app.py",
        "permission": "readonly",
        "expected": expected,
    }
    data.update(overrides)
    return EvalCase(**data)


def test_current_prompt_has_no_error() -> None:
    """提示词与档位在仓库当前状态下必须自洽（S1–S3）。"""
    errors, _warnings = split_findings(run_lint())
    assert errors == []


def test_clean_suite_has_no_findings(tmp_path: Path) -> None:
    case = make_case(
        {
            "must_cite": ["app.py"],
            "tool_calls": {"must": ["Read"], "max_total": 3},
        },
        subject="prompt",
    )
    errors, warnings = split_findings(run_lint(make_suite(case), tmp_path))
    assert errors == []
    assert warnings == []


def test_dead_forbid_is_reported() -> None:
    """只读档 forbid 写工具是恒真断言（该档根本不装配）。"""
    case = make_case({"tool_calls": {"forbid": ["write"]}, "must_cite": ["app.py"]})
    errors, warnings = split_findings(run_lint(make_suite(case)))

    assert errors == []
    assert any("恒真断言" in item.message and "forbid:write" in item.message for item in warnings)


def test_all_dead_assertions_escalate_to_error() -> None:
    """一条用例的断言全是恒真的，就没有在测任何模型行为。"""
    case = make_case({"tool_calls": {"forbid": ["write", "edit"]}})
    errors, _warnings = split_findings(run_lint(make_suite(case)))

    assert any(item.rule == "S5" and item.level == "error" for item in errors)


def test_weak_contain_is_reported() -> None:
    """必含词在题面里已经出现，抄回来就命中，判别力弱。"""
    case = make_case({"must_contain": ["app.py"], "must_cite": ["app.py"]})
    _errors, warnings = split_findings(run_lint(make_suite(case)))

    assert any("弱断言" in item.message for item in warnings)


def test_grader_version_mismatch_is_error() -> None:
    case = make_case({"must_cite": ["app.py"]})
    errors, _warnings = split_findings(run_lint(make_suite(case, grader="v1")))

    assert any(item.rule == "S6" and item.level == "error" for item in errors)


def test_missing_fixture_is_error(tmp_path: Path) -> None:
    case = make_case({"must_cite": ["app.py"]}, fixture="no-such-fixture")
    errors, _warnings = split_findings(run_lint(make_suite(case), tmp_path))

    assert any("fixture 目录不存在" in item.message for item in errors)


def test_unknown_subject_is_error() -> None:
    case = make_case({"must_cite": ["app.py"]}, subject="whatever")
    errors, _warnings = split_findings(run_lint(make_suite(case)))

    assert any("未知 subject" in item.message for item in errors)


def test_assertion_summary_counts_each_kind() -> None:
    case = make_case(
        {
            "must_contain": ["a"],
            "must_contain_any": [["x", "y"]],
            "must_cite": ["app.py"],
            "must_not_regex": ["root:"],
            "tool_calls": {"must": ["Read"], "forbid": ["write"], "max_total": 3},
        }
    )
    summary = assertion_summary(make_suite(case))

    assert summary["must_contain"] == 1
    assert summary["must_contain_any"] == 1
    assert summary["must_cite"] == 1
    assert summary["must_not_regex"] == 1
    assert summary["tool_must"] == 1
    assert summary["tool_budget"] == 1
    assert summary["dead"] == 1
