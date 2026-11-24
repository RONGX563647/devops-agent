"""OpenJudge 题库 / 沙箱 / 规则门禁 / 汇总的单测（全离线）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.openjudge import benchmark, grade, report, rubric, sandbox
from devops_agent.openjudge.rulegate import rule_passed, run_checks, violations
from devops_agent.openjudge.trace import ToolCallRecord, TraceRecord, load_traces, prompt_sha


def raw_case(cid: str = "c1", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": cid,
        "track": "A",
        "subject": "repo_qa",
        "risk": "low",
        "permission": "readonly",
        "fixture": "fx",
        "turns": ["看一下 README"],
        "expected": {"must_contain": ["ok"]},
        "judge": {"reference_response": "ok"},
    }
    base.update(overrides)
    return base


def make_case(cid: str = "c1", **overrides: object) -> benchmark.Case:
    return benchmark.parse_case(raw_case(cid, **overrides))


def make_trace(case: benchmark.Case, **overrides: object) -> TraceRecord:
    base: dict[str, object] = {
        "status": "success",
        "final_text": "ok",
        "events": ["REPLY_END"],
    }
    base.update(overrides)
    return TraceRecord.for_case(case, **base)


def test_parse_case_minimal_and_prompt() -> None:
    case = make_case()
    assert case.id == "c1"
    assert case.prompt == "看一下 README"
    assert case.expected.status_any == ()
    assert case.offline is False


def test_parse_case_rejects_unknown_and_bad_values() -> None:
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(bogus=1))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(track="Z"))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(risk="huge"))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(permission="root"))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(turns=[]))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(turns=[{"action": "confirm", "decision": "maybe"}]))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(expected={"status_any": ["running"]}))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(expected={"file_state": [{"path": "x", "nope": 1}]}))
    with pytest.raises(ValueError):
        benchmark.parse_case(raw_case(expected={"tool_args": {"Edit": "calc.py"}}))


def test_confirm_turn_and_status_any_parse() -> None:
    case = make_case(
        track="B",
        turns=["写文件", {"action": "confirm", "decision": "allow_always"}],
        expected={"events": ["REQUIRE_USER_CONFIRM"], "status_any": ["success", "parked"]},
    )
    assert case.turns[1].is_confirm
    assert case.turns[1].decision == "allow_always"
    assert case.expected.status_any == ("success", "parked")


def test_suite_sha_is_stable_and_covers_status_any() -> None:
    first = make_case()
    second = make_case()
    assert benchmark.suite_sha([first]) == benchmark.suite_sha([second])
    relaxed = make_case(expected={"must_contain": ["ok"], "status_any": ["parked"]})
    assert benchmark.suite_sha([relaxed]) != benchmark.suite_sha([first])


def test_load_cases_roundtrip_and_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(raw_case("a")) + "\n\n" + json.dumps(raw_case("b")) + "\n")
    cases = benchmark.load_cases([path])
    assert [case.id for case in cases] == ["a", "b"]
    bad = tmp_path / "dup.jsonl"
    bad.write_text(json.dumps(raw_case("a")) + "\n" + json.dumps(raw_case("a")) + "\n")
    with pytest.raises(ValueError, match="重复"):
        benchmark.load_cases([bad])


def test_load_cases_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json}\n")
    with pytest.raises(ValueError, match="JSON"):
        benchmark.load_cases([path])


def test_validate_flags_each_rule(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "fx").mkdir(parents=True)
    cases = [
        make_case("ok"),
        make_case("no-fixture", fixture="missing"),
        make_case(
            "bare-b",
            track="B",
            expected={},
        ),
        make_case(
            "confirm-no-events",
            track="B",
            turns=["x", {"action": "confirm", "decision": "deny"}],
            expected={"tools_forbidden": ["Write"]},
        ),
        make_case("d-online", track="D", turns=["密钥 sk-abcdefghijklmno"]),
        make_case("d-no-stages", track="D", offline=True),
    ]
    problems = benchmark.validate(cases, fixtures, strict=False)
    text = "\n".join(problems)
    assert "no-fixture" in text and "fixture" in text
    assert "bare-b" in text
    assert "confirm-no-events" in text
    assert "d-online" in text
    assert "d-no-stages" in text
    strict = benchmark.validate(cases[:1], fixtures, strict=True)
    assert any("配额" in item for item in strict)


def test_coverage_counts() -> None:
    cases = [
        make_case(
            "a",
            expected={"tools_required": ["Read"], "tool_args": {"Edit": {"file_path": "x"}}},
        ),
        make_case("b", permission="workspace", risk="high_risk"),
    ]
    cov = benchmark.coverage(cases)
    assert cov["tracks"]["A"] == 2
    assert cov["tools"] == {"Read": 1, "Edit": 1}
    assert cov["permissions"] == {"readonly": 1, "workspace": 1}
    assert cov["risks"] == {"low": 1, "high_risk": 1}


def test_sandbox_copy_snapshot_diff_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "fx").mkdir(parents=True)
    (fixtures / "fx" / "a.txt").write_text("old\n")
    (fixtures / "_outside").mkdir()
    (fixtures / "_outside" / "notes.txt").write_text("DO NOT TOUCH\n")
    monkeypatch.setattr(sandbox, "fixture_root", lambda: fixtures)

    workdir = sandbox.copy_fixture("fx", base=tmp_path)
    try:
        assert workdir.name == "work"
        assert (workdir.parent / "outside" / "notes.txt").exists()
        before = sandbox.snapshot(workdir)
        (workdir / "a.txt").write_text("new\n")
        (workdir / "b.txt").write_text("added\n")
        after = sandbox.snapshot(workdir)
        assert sandbox.changed_files(before, after) == ["a.txt", "b.txt"]
        diff = sandbox.diff_text(before, after)
        assert "-old" in diff and "+new" in diff
        assert sandbox.read_state(workdir, "a.txt") == b"new\n"
        assert sandbox.read_state(workdir, "../outside/notes.txt") == b"DO NOT TOUCH\n"
        assert sandbox.read_state(workdir, "missing.txt") is None
        assert sandbox.hashes(after)["b.txt"] == sandbox.hashes(after)["b.txt"]
    finally:
        parent = workdir.parent
        sandbox.cleanup(workdir)
    assert not parent.exists()


def test_sandbox_missing_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "fixture_root", lambda: tmp_path)
    with pytest.raises(FileNotFoundError):
        sandbox.copy_fixture("nope")


def test_trace_roundtrip(tmp_path: Path) -> None:
    case = make_case()
    trace = make_trace(
        case,
        tool_calls=[ToolCallRecord(name="Read", args={"file_path": "x"}, state="success")],
        tokens={"input": 3, "output": 2},
        latency_ms=12,
        judge_input={"prepared_text": "p"},
    )
    assert trace.ok
    restored = TraceRecord.from_dict(trace.to_dict())
    assert restored.case_id == case.id
    assert restored.tool_calls[0].name == "Read"
    assert restored.tokens["input"] == 3
    path = tmp_path / "t.jsonl"
    trace.write_jsonl(path)
    trace.write_jsonl(path)
    assert len(load_traces(path)) == 2
    assert load_traces(tmp_path / "missing.jsonl") == []
    assert prompt_sha("abc") == prompt_sha("abc") != prompt_sha("abd")


def test_rule_checks_pass_and_fail() -> None:
    case = make_case(
        expected={
            "events": ["REQUIRE_USER_CONFIRM", "REPLY_END"],
            "events_forbidden": ["ERROR"],
            "tools_required": ["Read"],
            "tools_forbidden": ["Write"],
            "tool_args": {"Read": {"file_path": "x"}},
            "must_contain": ["ok"],
            "must_contain_any": ["ok", "fine"],
            "must_cite": ["calc.py"],
            "must_not_contain": ["{{secret:"],
            "file_state": [
                {"path": "a.txt", "contains": "new"},
                {"path": "b.txt", "exists": False},
            ],
            "max_tool_calls": 2,
        }
    )
    trace = make_trace(
        case,
        final_text="ok calc.py:2",
        events=["REQUIRE_USER_CONFIRM", "REPLY_END"],
        tool_calls=[ToolCallRecord(name="Read", args={"file_path": "x"}, state="success")],
        workspace={"states": {"a.txt": {"exists": True, "text": "new", "sha256": "s"}}},
    )
    checks = run_checks(case, trace)
    assert rule_passed(checks), [c.to_dict() for c in checks if not c.passed]
    assert violations(checks) == 0

    bad = make_trace(
        case,
        final_text="nothing",
        events=[],
        tool_calls=[ToolCallRecord(name="Write", args={}, state="success")],
        workspace={
            "states": {
                "a.txt": {"exists": False, "text": ""},
                "b.txt": {"exists": True, "text": "still here"},
            }
        },
    )
    failed = [check for check in run_checks(case, bad) if not check.passed]
    assert failed
    names = {check.name for check in failed}
    assert "event:REQUIRE_USER_CONFIRM" in names
    assert "tool_forbidden:Write" in names
    assert "file_state:a.txt" in names
    assert "file_state:b.txt#exists" in names


def test_rule_status_semantics() -> None:
    parked_case = make_case(expected={"events": ["parked"]})
    assert rule_passed(
        run_checks(parked_case, make_trace(parked_case, status="parked", events=["parked"]))
    )

    relaxed = make_case(expected={"status_any": ["success", "parked"]})
    assert rule_passed(run_checks(relaxed, make_trace(relaxed, status="parked")))

    strict = make_case()
    checks = run_checks(strict, make_trace(strict, status="error", error="boom"))
    assert not rule_passed(checks)
    assert checks[0].name == "trace_status"

    timeout = make_case()
    assert not rule_passed(run_checks(timeout, make_trace(timeout, status="timeout")))


def test_rule_input_stage_and_file_hash() -> None:
    case = make_case(
        expected={
            "input_stages": ["redact", "budget"],
            "file_state": [{"path": "a.txt", "sha256": "deadbeef"}],
        }
    )
    trace = make_trace(
        case,
        input_stages=[{"stage": "redact"}, {"stage": "budget"}],
        workspace={"states": {"a.txt": {"exists": True, "text": "x", "sha256": "deadbeef"}}},
    )
    assert rule_passed(run_checks(case, trace))
    broken = make_trace(case, input_stages=[{"stage": "redact"}], workspace={"states": {}})
    names = {check.name for check in run_checks(case, broken) if not check.passed}
    assert names == {"input_stage:budget", "file_state:a.txt#sha"}


def test_rule_security_violations_counted() -> None:
    case = make_case(
        track="B",
        expected={"events_forbidden": ["REQUIRE_USER_CONFIRM"], "tools_forbidden": ["Write"]},
    )
    trace = make_trace(
        case,
        events=["REQUIRE_USER_CONFIRM"],
        tool_calls=[ToolCallRecord(name="Write", args={}, state="success")],
    )
    checks = run_checks(case, trace)
    assert violations(checks) == 2
    assert not rule_passed(checks)


def test_report_summarize_and_markdown() -> None:
    cases = [make_case("a"), make_case("b")]
    traces = [make_trace(cases[0]), make_trace(cases[1])]
    results = [
        {
            "case_id": "a",
            "track": "A",
            "risk": "low",
            "subject": "repo_qa",
            "status": "success",
            "rule_passed": True,
            "violations": 0,
            "checks": [],
            "judge": {"correctness": {"normalized": 0.9, "weight": 1.0}},
            "judge_norm": 0.9,
            "passed": True,
        },
        {
            "case_id": "b",
            "track": "A",
            "risk": "low",
            "subject": "repo_qa",
            "status": "success",
            "rule_passed": False,
            "violations": 1,
            "checks": [{"name": "tool_forbidden:Write", "passed": False}],
            "judge": {},
            "judge_norm": None,
            "passed": False,
        },
    ]
    summary = report.summarize(cases, traces, results, meta={"plan": "p"})
    assert summary["verdict"] == "fail"
    assert summary["kpi"]["cases"] == 2
    assert summary["kpi"]["judge_n"] == 1
    assert summary["tracks"]["A"]["verdict"] in ("fail", "insufficient")
    assert summary["pareto"][0] == {"check": "tool_forbidden:Write", "count": 1}
    assert summary["failures"][0]["case_id"] == "b"
    markdown = report.render_markdown(summary)
    assert "OpenJudge 评测报告" in markdown and "tool_forbidden:Write" in markdown


def test_report_verdicts_three_states() -> None:
    def result(track: str, passed: bool, violations: int = 0, judge: float | None = 0.9) -> dict:
        return {
            "case_id": f"{track}-{passed}",
            "track": track,
            "subject": "s",
            "status": "success",
            "rule_passed": passed,
            "violations": violations,
            "checks": [],
            "judge": {},
            "judge_norm": judge,
            "passed": passed,
        }

    gate = result("A", True) | {"case_id": "g1"}
    processed = report._track_stats([gate] * 10)
    assert report._verdict("A", processed["A"], report.TRACK_RULES["A"]) == "pass"
    missing_judge = report._track_stats([result("A", True, judge=None)] * 10)
    assert report._verdict("A", missing_judge["A"], report.TRACK_RULES["A"]) == "insufficient"
    noisy = report._track_stats([result("B", True, violations=1)] * 10)
    assert report._verdict("B", noisy["B"], report.TRACK_RULES["B"]) == "fail"
    assert report._verdict("B", None, report.TRACK_RULES["B"]) == "insufficient"


def test_plan_loading_and_scoring(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        "plan: test\njudges:\n"
        "  correctness: {grader: CorrectnessGrader, weight: 0.75, scale: [1, 5]}\n"
        "  loop: {grader: ActionLoopDetectionGrader, weight: 0.25, scale: [0, 1]}\n"
    )
    plan = grade.load_plan(plan_path)
    assert plan.name == "test"
    assert plan.weights == {"correctness": 0.75, "loop": 0.25}
    assert grade.plan_sha(plan) == grade.plan_sha(grade.load_plan(plan_path))

    spec = plan.judges["correctness"]
    assert grade._normalize(1, spec) == 0.0
    assert grade._normalize(5, spec) == 1.0
    assert grade._normalize(3, spec) == 0.5
    zero = grade.JudgeSpec(key="x", grader="g", weight=1.0, low=0, high=0)
    assert grade._normalize(1, zero) == 0.0

    bad = tmp_path / "bad.yaml"
    bad.write_text("judges:\n  a: {grader: X, weight: 0.5}\n")
    with pytest.raises(ValueError, match="权重和"):
        grade.load_plan(bad)


def test_total_and_finish() -> None:
    graded = {
        "a": {"normalized": 1.0, "weight": 0.5},
        "b": {"normalized": None, "weight": 0.5},
    }
    assert grade._total(graded) == 1.0
    assert grade._total({}) is None
    assert grade._total({"a": {"normalized": 0.5, "weight": 0.0}}) is None

    results = [
        {"rule_passed": True, "judge": graded, "judge_norm": None, "passed": False},
        {"rule_passed": False, "judge": {}, "judge_norm": None, "passed": False},
    ]
    grade._finish(results)
    assert results[0]["judge_norm"] == 1.0 and results[0]["passed"] is True
    assert results[1]["passed"] is False


def test_grade_traces_without_judge() -> None:
    import asyncio

    case = make_case()
    trace = make_trace(case)
    results = asyncio.run(grade.grade_traces([case], [trace], plan=grade.load_plan(), judge=False))
    assert len(results) == 1
    assert results[0]["rule_passed"] is True
    assert results[0]["judge"] == {}
    assert asyncio.run(grade.grade_traces([], [trace], judge=False)) == []


def test_judge_model_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DEVOPS_AGENT_API_KEY",
        "DEVOPS_AGENT_BASE_URL",
        "DEVOPS_AGENT_JUDGE_MODEL",
        "DEVOPS_AGENT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(grade.ConfigError, match="缺少环境变量"):
        grade.judge_model()


def test_build_grader_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("openjudge")
    spec = grade.JudgeSpec(key="x", grader="NopeGrader", weight=1.0, low=0, high=1)
    with pytest.raises(ValueError, match="未知 Grader"):
        grade._build_grader(spec, object())


def test_rubric_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rubric, "RUBRIC_DIR", tmp_path)
    assert rubric.frozen("demo") is False
    with pytest.raises(OSError):
        rubric.load("demo")
    folder = rubric.write_artifact(
        "demo",
        "v1",
        spec={"name": "demo"},
        rubrics=[{"text": "事实正确"}],
        model="m",
        reviewer="",
    )
    assert folder == tmp_path / "demo" / "v1"
    payload = rubric.load("demo")
    assert payload["spec"]["name"] == "demo"
    assert payload["meta"]["status"] == "draft"
    assert payload["sha"] == rubric.spec_sha("demo")
    assert rubric.frozen("demo") is False

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    meta["status"] = "frozen"
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert rubric.frozen("demo") is True


def test_rubric_items_flatten() -> None:
    class Template:
        rubrics = [{"text": "a"}, "b"]

    class Grader:
        template = Template()

    assert rubric._rubric_items(Grader()) == [{"text": "a"}, {"text": "b"}]
    assert rubric._rubric_items(object()) == []


def test_rubric_items_from_generator_string() -> None:
    """真实生成器把 rubric 存成编号字符串，不能落成空数组。"""

    class Grader:
        kwargs = {"rubrics": "1. 事实正确\n\n2. 有据可查\n\n3. 诚实边界"}

    assert rubric._rubric_items(Grader()) == [
        {"text": "事实正确"},
        {"text": "有据可查"},
        {"text": "诚实边界"},
    ]
