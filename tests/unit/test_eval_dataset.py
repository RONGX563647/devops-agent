"""数据集模型、指纹与校验规则单测（全离线）。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from devops_agent.eval import cli
from devops_agent.eval.dataset import (
    EvalCase,
    Manifest,
    Suite,
    load_suite,
    scenario_of,
    suite_sha256,
)
from devops_agent.eval.validation import split_warnings, validate_suite


def make_case(
    case_id: str = "c-1",
    scenario: str = "common",
    *,
    tags: list[str] | None = None,
    expected: dict[str, Any] | None = None,
    judge: dict[str, Any] | None = None,
) -> EvalCase:
    return EvalCase(
        id=case_id,
        tags=tags if tags is not None else [f"scenario:{scenario}"],
        input="检查仓库",
        expected=expected if expected is not None else {"must_contain": ["ok"]},
        judge=judge,
    )


def write_suite(
    directory: Path,
    cases: list[EvalCase],
    *,
    min_cases: int = 40,
    sha: str | None = None,
    counts: dict[str, int] | None = None,
) -> Path:
    """把用例与清单写到临时目录，返回 jsonl 路径。"""
    path = directory / "demo.v1.jsonl"
    path.write_text(
        "\n".join(json.dumps(case.model_dump(mode="json"), ensure_ascii=False) for case in cases),
        encoding="utf-8",
    )
    actual = Counter(scenario for case in cases if (scenario := scenario_of(case)) is not None)
    manifest = Manifest(
        suite="demo",
        version="1.0.0",
        sha256=sha if sha is not None else suite_sha256(cases),
        counts=counts if counts is not None else dict(actual),
        min_cases_for_gate=min_cases,
        thresholds={"pass_rate": 0.8},
    )
    (directory / "demo.v1.manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    return path


def test_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EvalCase.model_validate(
            {
                "id": "c",
                "tags": ["scenario:common"],
                "input": "x",
                "expected": {"must_contain": ["ok"]},
                "unknown": True,
            }
        )


def test_sha_is_order_independent_and_manifest_roundtrip(tmp_path: Path) -> None:
    first = make_case("c-1")
    second = make_case("c-2", expected={"must_contain": ["done"]})
    assert suite_sha256([first, second]) == suite_sha256([second, first])

    path = write_suite(tmp_path, [first, second], min_cases=40)
    suite = load_suite(path)
    assert [case.id for case in suite.cases] == ["c-1", "c-2"]
    assert suite.manifest.counts == {"common": 2}

    messages = validate_suite(suite)
    errors, warnings = split_warnings(messages)
    assert errors == []
    assert warnings and "min_cases_for_gate" in warnings[0]


def test_sha_and_counts_mismatch_are_errors(tmp_path: Path) -> None:
    case = make_case("c-1")
    path = write_suite(tmp_path, [case], sha="deadbeef", counts={"variant": 1})
    messages = validate_suite(load_suite(path))
    joined = "\n".join(messages)
    assert "manifest.sha256 不匹配" in joined
    assert "manifest.counts 与实际不一致" in joined


def test_ratio_is_enforced_once_sample_is_large_enough(tmp_path: Path) -> None:
    cases = [make_case(f"c-{index}") for index in range(10)]
    path = write_suite(tmp_path, cases, min_cases=10)
    messages = validate_suite(load_suite(path))
    joined = "\n".join(messages)
    assert "scenario:common 占比 100.0%" in joined
    assert "scenario:variant 占比" in joined


def test_small_sample_only_warns(tmp_path: Path) -> None:
    case = make_case("c-1", scenario="adversarial")
    path = write_suite(tmp_path, [case], min_cases=40)
    errors, warnings = split_warnings(validate_suite(load_suite(path)))
    assert errors == []
    assert warnings and "warning:" in warnings[0]


def test_duplicate_tags_assertions_and_judge_dimensions(tmp_path: Path) -> None:
    duplicate = make_case("dup")
    empty_tags = make_case("no-tags", tags=[])
    no_scenario = make_case("no-scenario", tags=["domain:repo"])
    no_assertion = make_case("no-assert", expected={"must_contain": []})
    bad_judge = make_case(
        "bad-judge",
        judge={"rubric": "peem.v1", "dimensions": ["correctness", "vibes"]},
    )
    path = write_suite(
        tmp_path,
        [duplicate, duplicate, empty_tags, no_scenario, no_assertion, bad_judge],
        min_cases=40,
    )
    joined = "\n".join(validate_suite(load_suite(path)))
    assert "duplicate case id: dup" in joined
    assert "no-tags: tags 不能为空" in joined
    assert "no-scenario: 缺少 scenario:* 标签" in joined
    assert "no-assert: expected 至少需要一条断言" in joined
    assert "bad-judge: 未知 judge 维度: vibes" in joined


def test_cli_validate_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_suite(tmp_path, [make_case("c-1")], min_cases=40)
    assert cli.main(["validate", "--suite", str(path)]) == 0
    assert "ok 1 cases" in capsys.readouterr().out

    bad = write_suite(tmp_path, [make_case("c-1")], min_cases=40, sha="bad")
    assert cli.main(["validate", "--suite", str(bad)]) == 1


def test_suite_model_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Suite.model_validate(
            {
                "manifest": {
                    "suite": "x",
                    "version": "1",
                    "sha256": "x",
                    "counts": {},
                },
                "cases": [],
                "extra": 1,
            }
        )
