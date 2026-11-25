"""prompt_lint 的验收测试：四条规则各造一个「已证实的缺陷」，并锁住 CLI 退出码。

规则的价值在于「能抓住真实事故」，所以除了构造对象，还用真实 PROFILES 跑一遍，
保证提示词与档位没漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.governance.profiles import PROFILES, PermissionProfile
from devops_agent.openjudge.benchmark import Case, parse_case
from devops_agent.prompt_lint import cli, rules
from devops_agent.prompt_lint.known_issues import apply_known_issues
from devops_agent.prompt_lint.rules import (
    Finding,
    check_duplicate_prompts,
    check_placeholders,
    check_tool_closure,
    check_write_claims,
    normalize_prompt,
    run_lint,
    split_findings,
)
from devops_agent.tools.catalog import READONLY_TOOL_NAMES


def make_case(case_id: str, prompt: str) -> Case:
    """最小合法用例：只填 benchmark 必填字段。"""
    return parse_case(
        {
            "id": case_id,
            "track": "A",
            "risk": "low",
            "permission": "readonly",
            "turns": [prompt],
        }
    )


def make_profile(**overrides: object) -> PermissionProfile:
    data: dict[str, object] = {
        "key": "workspace",
        "label": "工作区可写",
        "mode": "accept_edits",
        "tools": READONLY_TOOL_NAMES,
        "fallback": "ask",
        "note": "可在工作区内建/删/改文件。",
    }
    data.update(overrides)
    return PermissionProfile(**data)  # type: ignore[arg-type]


# ------------------------------------------------------------ P3 写能力承诺


def test_write_claim_without_write_tools_is_blocking() -> None:
    """档位说明说「可写」，但工具集只有只读四件套 → blocking。

    这正是「权限切了但毫无变化」的事故形态：模型照着说明去写，被权限层拒绝。
    """
    findings = check_write_claims([make_profile()])
    assert [(item.rule, item.level) for item in findings] == [("P3", "error")]


def test_write_tools_without_claim_is_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向：挂了写工具但档位说明没告知 → 模型退回「只讲解不修改」。"""
    monkeypatch.setattr(rules, "policy_note", lambda _profile: "本会话可执行命令。")
    profile = make_profile(tools=(*READONLY_TOOL_NAMES, "write", "edit"), key="execute")
    findings = check_write_claims([profile])
    assert [(item.rule, item.level) for item in findings] == [("P3", "error")]


def test_readonly_profile_is_not_flagged() -> None:
    """只读档写「不要尝试 Edit/Write」是拒绝语，不是承诺。"""
    assert check_write_claims([PROFILES["readonly"]]) == []


def test_real_profiles_have_no_write_claim_findings() -> None:
    """真实档位是文案与能力矩阵的唯一来源，漂移必须在这里被抓住。"""
    assert check_write_claims(list(PROFILES.values())) == []


# ------------------------------------------------------------ P2 占位符


def test_unknown_placeholder_is_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """模板里出现 `{tool_prompt}` 这类写错的键，渲染时永远填不上。"""
    monkeypatch.setattr(rules, "_TOOL_PROMPT", "用 {tool_prompt} 描述工具，工作目录 {workdir}")
    findings = check_placeholders()
    ruleset = {item.rule for item in findings}
    assert ruleset == {"P2"}
    assert any("tool_prompt" in item.message for item in findings)


def test_malformed_braces_are_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """占位符被删掉一半（单个花括号）时 Formatter 直接解析失败。"""
    monkeypatch.setattr(rules, "_TOOL_PROMPT", "工作目录 {workdir")
    findings = check_placeholders()
    assert [item.rule for item in findings] == ["P2"]


def test_absolute_path_in_prompt_is_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """硬编码机器路径会让同一 prompt_sha 下的下发文本因机器而异。"""
    monkeypatch.setattr(rules, "_TOOL_PROMPT", "工作目录 C:\\work\\repo，shell {shell}")
    findings = check_placeholders()
    assert any("绝对路径" in item.message for item in findings)


def test_real_prompt_has_no_placeholder_findings() -> None:
    assert check_placeholders() == []


# ------------------------------------------------------------ P1 工具闭包


def test_tool_without_mention_is_blocking() -> None:
    """挂了模型从没见过的工具 = 白挂。"""
    profile = make_profile(tools=(*READONLY_TOOL_NAMES, "wat"))
    findings = check_tool_closure([profile])
    assert [item.rule for item in findings] == ["P1"]
    assert "wat" in findings[0].message


def test_real_profiles_mention_every_tool() -> None:
    assert check_tool_closure(list(PROFILES.values())) == []


# ------------------------------------------------------------ P4 题面重复


def test_duplicate_prompts_are_reported() -> None:
    first = make_case("a-1", "检查 calc.py 的 add")
    second = make_case("a-2", "检查   calc.py 的 add\n")
    findings = check_duplicate_prompts([first, second])
    assert [item.where for item in findings] == ["case:a-1", "case:a-2"]
    assert all(item.rule == "P4" and item.level == "error" for item in findings)


def test_distinct_prompts_are_clean() -> None:
    cases = [make_case("a-1", "检查 calc.py"), make_case("a-2", "检查 app.yaml")]
    assert check_duplicate_prompts(cases) == []


def test_normalize_collapses_whitespace_only() -> None:
    """大小写不折叠：代码片段里的大小写差异是真差异。"""
    assert normalize_prompt("  a  b\n c ") == "a b c"
    assert normalize_prompt("Add") != normalize_prompt("add")


# ------------------------------------------------------------ 白名单与退出码


def _write_case_file(tmp_path: Path) -> Path:
    path = tmp_path / "cases.jsonl"
    row = {"id": "a-1", "track": "A", "risk": "low", "permission": "readonly", "turns": ["hi"]}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def test_known_issue_is_waived_not_blocking() -> None:
    known = {("P3", "profile:workspace")}
    errors = [Finding("P3", "error", "profile:workspace", "承诺可写")]
    blocking, accepted, stale = apply_known_issues(errors, known)
    assert blocking == []
    assert len(accepted) == 1
    assert stale == []


def test_stale_known_issue_is_reported() -> None:
    blocking, accepted, stale = apply_known_issues([], {("P3", "profile:workspace")})
    assert (blocking, accepted, stale) == ([], [], [("P3", "profile:workspace")])


def test_cli_exit_zero_when_whitelisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """白名单命中的 error 不进 blocking，退出码 0。"""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"accepted": [{"rule": "P3", "where": "profile:workspace"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "run_lint",
        lambda **_kwargs: [Finding("P3", "error", "profile:workspace", "承诺可写")],
    )
    code = cli.main(
        ["--json", "--cases", str(_write_case_file(tmp_path)), "--known-issues", str(baseline)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["blocking"] == 0
    assert payload["waived"] == 1


def test_cli_exit_one_when_not_whitelisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"accepted": []}), encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "run_lint",
        lambda **_kwargs: [Finding("P3", "error", "profile:workspace", "承诺可写")],
    )
    code = cli.main(
        ["--json", "--cases", str(_write_case_file(tmp_path)), "--known-issues", str(baseline)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["blocking"] == 1


def test_cli_exit_one_when_whitelist_item_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """修好的豁免还留在清单里 → 退出码 1（与旧 eval lint 的 `blocking or stale` 一致）。"""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"accepted": [{"rule": "P3", "where": "profile:workspace"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "run_lint", lambda **_kwargs: [])
    code = cli.main(
        ["--json", "--cases", str(_write_case_file(tmp_path)), "--known-issues", str(baseline)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["blocking"] == 1
    assert payload["findings"][0]["rule"] == "baseline"


# ------------------------------------------------------------ 真实仓库


def test_real_repo_payload_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """在仓库上真跑一次：JSON 结构固定，退出码只由 blocking 决定。"""
    code = cli.main(["--json", "--known-issues", str(cli.DEFAULT_KNOWN_ISSUES)])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"findings", "blocking", "advice", "waived"}
    assert code == (1 if payload["blocking"] else 0)


def test_split_findings_orders_by_level() -> None:
    items = [Finding("P4", "warning", "case:x", "w"), Finding("P1", "error", "p", "e")]
    errors, advice = split_findings(items)
    assert [item.level for item in errors] == ["error"]
    assert [item.level for item in advice] == ["warning"]
    ordered = run_lint(cases=[])
    expected = sorted(ordered, key=lambda item: (item.level != "error", item.rule, item.where))
    assert ordered == expected
