"""执行器单测：事件累计、fixture 复制、超时、挂起与裁判降级（全离线）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from devops_agent.eval.dataset import EvalCase, Manifest, Suite
from devops_agent.eval.graders import TraceResult
from devops_agent.eval.runner import execute_case, run_suite


class FakeRuntime:
    """按预设事件序列吐 dict 的假运行时。"""

    def __init__(self, events: list[dict[str, Any]], delay: float = 0.0) -> None:
        self._events = events
        self._delay = delay

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[dict[str, Any]]:
        if self._delay:
            await asyncio.sleep(self._delay)
        for event in self._events:
            yield event


class StubJudge:
    """记录调用次数并返回固定分数。"""

    def __init__(self, scores: dict[str, float] | None = None, error: Exception | None = None):
        self.scores = scores or {"correctness": 4.0}
        self.error = error
        self.calls = 0

    def score(self, case: EvalCase, result: TraceResult) -> dict[str, float]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.scores


def make_case(expected: dict[str, Any], **overrides: Any) -> EvalCase:
    data: dict[str, Any] = {
        "id": "case-1",
        "tags": ["scenario:common"],
        "input": "检查仓库",
        "fixture": None,
        "timeout_s": 5,
        "expected": expected,
    }
    data.update(overrides)
    return EvalCase(**data)


def test_events_are_accumulated_and_tempdir_is_cleaned() -> None:
    events = [
        {"type": "TEXT_BLOCK_DELTA", "delta": "hello "},
        {"type": "TOOL_CALL_START", "tool_call_name": "Read, Grep"},
        {"type": "TOOL_CALL_START", "tool_call_name": "read"},
        {"type": "MODEL_CALL_END", "input_tokens": 10, "output_tokens": 5},
        {"type": "REPLY_END", "input_tokens": 2, "output_tokens": 1},
    ]
    case = make_case(
        {
            "must_contain": ["hello"],
            "tool_calls": {"must": ["Read", "Grep"], "max_total": 3, "ordered": True},
        }
    )
    seen: list[Path] = []

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        seen.append(workdir)
        return FakeRuntime(events)

    score, trace, judge_scores = asyncio.run(execute_case(case, factory))
    assert score.passed
    assert judge_scores is None
    assert trace.final_text == "hello "
    assert trace.tool_names == ["Read", "Grep"]
    assert trace.tokens == 18
    assert trace.status == "success"
    assert seen and not seen[0].exists()
    assert trace.workspace_dir == seen[0]


def test_fixture_is_copied_and_preserved(tmp_path: Path) -> None:
    source = tmp_path / "fixtures" / "demo"
    source.mkdir(parents=True)
    (source / "hello.txt").write_text("fixture-content", encoding="utf-8")

    case = make_case(
        {
            "must_contain": ["fixture-content"],
            "file_state": [{"path": "hello.txt", "contains": "fixture-content"}],
        },
        fixture="demo",
    )
    seen: list[Path] = []

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        seen.append(workdir)
        content = (workdir / "hello.txt").read_text(encoding="utf-8")
        return FakeRuntime(
            [
                {"type": "TEXT_BLOCK_DELTA", "delta": content},
                {"type": "REPLY_END"},
            ]
        )

    score, trace, _ = asyncio.run(execute_case(case, factory, fixture_root=tmp_path / "fixtures"))
    assert score.passed
    assert (source / "hello.txt").exists()
    assert not seen[0].exists()


def test_timeout_marks_error() -> None:
    case = make_case({"must_contain": ["ok"]}, timeout_s=0)

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        return FakeRuntime([{"type": "TEXT_BLOCK_DELTA", "delta": "ok"}], delay=5)

    score, trace, _ = asyncio.run(execute_case(case, factory))
    assert trace.status == "error"
    assert trace.error is not None and "超时" in trace.error
    assert not score.passed
    assert "status" in score.hard_failures


def test_require_user_confirm_is_pending() -> None:
    case = make_case({"status": "success", "must_contain": ["ok"]})

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        return FakeRuntime(
            [
                {"type": "TEXT_BLOCK_DELTA", "delta": "ok"},
                {"type": "REQUIRE_USER_CONFIRM", "tool_call_name": "Bash"},
            ]
        )

    score, trace, _ = asyncio.run(execute_case(case, factory))
    assert trace.status == "pending"
    assert not score.passed
    assert "status" in score.hard_failures


def test_runtime_factory_error_is_captured() -> None:
    case = make_case({"must_contain": ["ok"]})

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        raise RuntimeError("boom")

    score, trace, _ = asyncio.run(execute_case(case, factory))
    assert trace.status == "error"
    assert trace.error == "boom"
    assert not score.passed


def test_judge_is_called_only_after_rule_pass() -> None:
    case = make_case(
        {"must_contain": ["ok"]},
        judge={"rubric": "peem.v1", "dimensions": ["correctness"]},
    )

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        return FakeRuntime([{"type": "TEXT_BLOCK_DELTA", "delta": "ok"}])

    judge = StubJudge({"correctness": 4.5})
    score, _trace, judge_scores = asyncio.run(execute_case(case, factory, judge=judge))
    assert score.passed
    assert judge_scores == {"correctness": 4.5}
    assert judge.calls == 1
    assert any(check.name == "judge" and check.ok for check in score.checks)

    failing_judge = StubJudge()
    score, _trace, judge_scores = asyncio.run(
        execute_case(make_case({"must_contain": ["missing"]}), factory, judge=failing_judge)
    )
    assert not score.passed
    assert failing_judge.calls == 0
    assert judge_scores is None


def test_judge_failure_degrades_to_none() -> None:
    case = make_case(
        {"must_contain": ["ok"]},
        judge={"rubric": "peem.v1", "dimensions": ["correctness"]},
    )

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        return FakeRuntime([{"type": "TEXT_BLOCK_DELTA", "delta": "ok"}])

    judge = StubJudge(error=RuntimeError("judge down"))
    score, _trace, judge_scores = asyncio.run(execute_case(case, factory, judge=judge))
    assert judge_scores is None
    assert score.passed  # 规则结论不被裁判故障推翻
    judge_check = next(check for check in score.checks if check.name == "judge")
    assert not judge_check.ok
    assert "judge down" in judge_check.detail


def test_run_suite_respects_limit() -> None:
    cases = [make_case({"status": "success"}, id=f"case-{index}") for index in range(3)]
    suite = Suite(
        manifest=Manifest(
            suite="demo",
            version="1",
            sha256="x",
            counts={"common": 3},
            min_cases_for_gate=1,
        ),
        cases=cases,
    )

    def factory(_case: EvalCase, workdir: Path) -> FakeRuntime:
        return FakeRuntime([{"type": "REPLY_END"}])

    runs = asyncio.run(run_suite(suite, factory, limit=2))
    assert [run.case.id for run in runs] == ["case-0", "case-1"]
    assert all(run.score.passed for run in runs)
