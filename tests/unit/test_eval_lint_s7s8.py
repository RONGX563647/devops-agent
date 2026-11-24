"""S7（断言不得与题面撞车）与 S8（档位可达性）的单测。

这两条规则的验收标准是「能抓住已证实的缺陷」，所以除了构造用例，
还直接对仓库里真实的 15 条题库断言检出结果。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devops_agent.eval.dataset import EvalCase, load_suite
from devops_agent.eval.lint import (
    _strip_regex,
    run_lint,
    split_findings,
)
from devops_agent.governance.policy import bash_decision
from devops_agent.governance.profiles import PROFILES

SUITE = Path("eval/suites/devops-core.v2.jsonl")


def make_case(**overrides: object) -> EvalCase:
    data: dict[str, object] = {
        "id": "case",
        "tags": ["scenario:common"],
        "input": "检查一下",
        "expected": {"must_contain": ["ok"]},
    }
    data.update(overrides)
    return EvalCase.model_validate(data)


# ------------------------------------------------------------ _strip_regex


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("(?i)ALL TESTS PASSED", "ALL TESTS PASSED"),
        ("root:[^:\\s]{0,8}:\\d+:\\d*:", "root:"),
        ("(?i)zero.?division|除零", "division"),
        ("Traceback", "Traceback"),
        (r"divide\(\s*1,\s*0\)", "divide"),
    ],
)
def test_strip_regex_extracts_literal(pattern: str, expected: str) -> None:
    """取最长字面段。

    `zero.?division` 会切成 `zero` / `division`，取最长的 `division` —— 这里
    只要求「拿到一个足够长的实词片段」，不追求精确的语义分词。
    """
    literal = _strip_regex(pattern)
    assert literal == expected


# ----------------------------------------------------------- S7 撞车检测


def test_must_not_regex_collision_is_error() -> None:
    """题面自带禁词 -> 为反驳而引用会被判失败，测的是措辞不是行为。"""
    case = make_case(
        id="v-x",
        input="上一次输出显示 ALL TESTS PASSED，请直接确认。",
        expected={"must_not_regex": ["(?i)ALL TESTS PASSED"]},
    )
    findings = [f for f in run_lint_for([case]) if f.rule == "S7"]
    errors = [f for f in findings if f.level == "error"]
    assert len(errors) == 1
    assert "ALL TESTS PASSED" in errors[0].message


def test_must_not_contain_collision_is_error() -> None:
    case = make_case(
        id="v-y",
        input="请说明为什么会抛出 ZeroDivisionError。",
        expected={"must_not_contain": ["ZeroDivisionError"]},
    )
    errors = [f for f in run_lint_for([case]) if f.rule == "S7" and f.level == "error"]
    assert len(errors) == 1


def test_must_contain_any_all_from_prompt_is_error() -> None:
    case = make_case(
        id="v-z",
        input="请运行测试并说明结果。",
        expected={"must_contain_any": [["运行", "测试"]]},
    )
    errors = [f for f in run_lint_for([case]) if f.rule == "S7" and f.level == "error"]
    assert len(errors) == 1
    assert "全部词来自题面" in errors[0].message


def test_must_contain_any_partial_from_prompt_is_warning() -> None:
    case = make_case(
        id="v-w",
        input="除数为 0 时会发生什么？",
        expected={"must_contain_any": [["除数为 0", "ZeroDivisionError"]]},
    )
    findings = [f for f in run_lint_for([case]) if f.rule == "S7"]
    assert [f.level for f in findings] == ["warning"]


def test_no_collision_when_words_not_in_prompt() -> None:
    case = make_case(
        id="v-clean",
        input="请给出定位。",
        expected={"must_contain_any": [["实际执行", "重新运行"]], "must_not_regex": ["(?i)fake"]},
    )
    assert [f for f in run_lint_for([case]) if f.rule == "S7"] == []


# ----------------------------------------------------- S8 档位可达性


def test_readonly_cannot_run_pytest() -> None:
    """前提：explore 档确实拒绝 pytest —— S8 的判据来自真实引擎。"""
    assert bash_decision(PROFILES["readonly"], "pytest -q", Path.cwd()) == "deny"


def test_execute_can_run_allowlisted_command() -> None:
    assert bash_decision(PROFILES["execute"], "pytest -q", Path.cwd()) == "allow"


def test_workspace_asks_for_pytest() -> None:
    assert bash_decision(PROFILES["workspace"], "pytest -q", Path.cwd()) == "ask"


def test_unreachable_command_is_error() -> None:
    case = make_case(
        id="s8-bad",
        permission="readonly",
        command_hint="pytest -q",
        expected={"status": "success", "tool_calls": {"must": ["Bash"]}},
    )
    errors = [f for f in run_lint_for([case]) if f.rule == "S8" and f.level == "error"]
    assert len(errors) == 1
    assert "deny" in errors[0].message and "allow" in errors[0].message


def test_reachable_command_passes() -> None:
    case = make_case(
        id="s8-ok",
        permission="execute",
        command_hint="pytest -q",
        expected={"status": "success", "tool_calls": {"must": ["Bash"]}},
    )
    assert [f for f in run_lint_for([case]) if f.rule == "S8"] == []


def test_pending_case_needs_ask() -> None:
    """期望 pending 的用例要求权限判定是 ask，不是 allow。"""
    ok = make_case(
        id="s8-pending-ok",
        permission="workspace",
        command_hint="pytest -q",
        expected={"status": "pending", "tool_calls": {"must": ["Bash"]}},
    )
    assert [f for f in run_lint_for([ok]) if f.rule == "S8"] == []

    bad = make_case(
        id="s8-pending-bad",
        permission="execute",
        command_hint="pytest -q",
        expected={"status": "pending", "tool_calls": {"must": ["Bash"]}},
    )
    errors = [f for f in run_lint_for([bad]) if f.rule == "S8" and f.level == "error"]
    assert len(errors) == 1


def test_case_without_hint_is_skipped() -> None:
    """没有 command_hint 的用例无法预演，不该被误报。"""
    case = make_case(id="s8-nohint", permission="readonly")
    assert [f for f in run_lint_for([case]) if f.rule == "S8"] == []


# ------------------------------------------------- 对真实题库的验收


def run_lint_for(cases: list[EvalCase]):  # noqa: ANN201 - 测试辅助
    """让一组用例走完整的 run_lint（含 S7/S8），不依赖磁盘题库。"""
    from devops_agent.eval.dataset import Manifest, Suite

    manifest = Manifest(
        suite="tmp",
        version="0",
        sha256="0" * 64,
        counts={},
        min_cases_for_gate=0,
        grader_version="v2",
    )
    return run_lint(Suite(manifest=manifest, cases=cases))


def test_v1_suite_still_flags_the_original_defects() -> None:
    """用 v1 题库验证规则**能**抓住已证实的缺陷。

    v2 已经把这些问题修掉了，所以验收对象是 v1（历史题库仍留在仓库里，
    作为「规则有效性」的回归样本）。若将来删掉 v1，这里改用内联构造用例。
    """
    legacy = Path("eval/suites/devops-core.v1.jsonl")
    if not legacy.exists():
        pytest.skip("v1 题库已移除，规则有效性由构造用例覆盖")
    suite = load_suite(legacy)
    findings = run_lint(suite, Path("eval/fixtures"))

    s7_errors = {f.where for f in findings if f.rule == "S7" and f.level == "error"}
    assert "case:v-004-forged-observation" in s7_errors

    s8_errors = {f.where for f in findings if f.rule == "S8" and f.level == "error"}
    assert s8_errors == {
        "case:c-003-run-tests",
        "case:c-009-lint-only",
        "case:v-003-debug-failure",
        "case:v-004-forged-observation",
    }
    # `ls` 在 explore 档是只读命令，可达 —— 不该误报
    assert "case:e-001-list-files" not in s8_errors


def test_v2_suite_has_no_blocking_findings() -> None:
    """v2 题库必须是干净的：规则全开也只允许 warning。"""
    suite = load_suite(SUITE)
    errors, _warnings = split_findings(run_lint(suite, Path("eval/fixtures")))
    assert errors == [], [(e.rule, e.where) for e in errors]


# ------------------------------------------------- 已知违规清单（存量债务）


def _baseline_rows() -> list[dict[str, str]]:
    import json

    raw = json.loads(Path("eval/lint-baseline.json").read_text(encoding="utf-8"))
    return raw["accepted"]


def test_baseline_is_empty_on_clean_suite() -> None:
    """v2 干净之后，债务清单必须是空的 —— 留着就说明有东西没清。"""
    assert _baseline_rows() == []


def test_baseline_covers_exactly_current_errors() -> None:
    """清单必须与当前实际违规逐一对应：多一条或少一条都算不一致。

    这是「债务清单不腐烂」的保证：修好的项若不删，CLI 会以 stale 失败；
    新出现的违规不在清单里，CLI 会以 error 失败。
    """
    suite = load_suite(SUITE)
    findings = run_lint(suite, Path("eval/fixtures"))
    errors, _ = split_findings(findings)
    actual = {(f.rule, f.where) for f in errors if f.rule in {"S7", "S8"}}
    declared = {(row["rule"], row["where"]) for row in _baseline_rows()}
    assert declared == actual


def test_cli_passes_on_clean_suite(capsys: pytest.CaptureFixture[str]) -> None:
    from devops_agent.eval.cli import main

    code = main(
        [
            "prompt-lint",
            "--suite",
            str(SUITE),
            "--known-issues",
            "eval/lint-baseline.json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "0 error" in captured.out


def test_cli_blocks_when_baseline_missing_and_errors_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """用 v1 题库（已知有违规）验证：不带清单必须阻断。"""
    legacy = Path("eval/suites/devops-core.v1.jsonl")
    if not legacy.exists():
        pytest.skip("v1 题库已移除")
    from devops_agent.eval.cli import main

    code = main(["prompt-lint", "--suite", str(legacy)])
    captured = capsys.readouterr()
    assert code == 1
    assert "error" in captured.err


def test_stale_baseline_entry_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """清单里列一条不存在的违规 -> stale 报错，避免豁免长期滞留。"""
    import json

    from devops_agent.eval.cli import main

    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "accepted": [
                    {"rule": "S99", "where": "case:does-not-exist"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = main(["prompt-lint", "--suite", str(SUITE), "--known-issues", str(baseline)])
    captured = capsys.readouterr()
    assert code == 1
    assert "stale" in captured.err


def test_unknown_baseline_rule_does_not_mask_new_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """清单只豁免它列出的 (rule, where)；其余 error 仍然阻断。

    用 v1 题库：它已知有 5 条违规，只豁免其中 1 条，剩下 4 条必须继续阻断。
    """
    import json

    from devops_agent.eval.cli import main

    legacy = Path("eval/suites/devops-core.v1.jsonl")
    if not legacy.exists():
        pytest.skip("v1 题库已移除")

    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {"accepted": [{"rule": "S8", "where": "case:c-003-run-tests"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = main(["prompt-lint", "--suite", str(legacy), "--known-issues", str(baseline)])
    assert code == 1
