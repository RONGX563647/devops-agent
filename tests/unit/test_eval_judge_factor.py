"""裁判因子与裁判覆盖范围的单测。

锁两个已证实的缺陷：
- `cli._factors` 读的是不存在的 `JUDGE_MODEL`，而 `judge.py` 读
  `DEVOPS_AGENT_JUDGE_MODEL` —— 报告里的 `judge_model` 恒等于主模型，
  而它是可比性检查的阻断因子：换了裁判模型仍判「可比」，delta 被误当结论。
- `_score_with_judge` 在规则未通过时直接跳过裁判，退化会让难用例退出分母，
  `judge_correctness` 反而上升（实测 pass_rate −13pp、judge +1.6）。
"""

from __future__ import annotations

import pytest

from devops_agent.eval.dataset import EvalCase, JudgeSpec, load_suite
from devops_agent.eval.graders import CaseScore, TraceResult
from devops_agent.eval.judge import (
    JUDGE_MODEL_ENV,
    MODEL_ENV,
    OpenAIJudge,
    resolve_judge_model,
)
from devops_agent.eval.report import compare
from devops_agent.eval.runner import _score_with_judge


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (MODEL_ENV, JUDGE_MODEL_ENV, "JUDGE_MODEL"):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------- resolve_judge_model


def test_resolve_prefers_dedicated_judge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ENV, "main-model")
    monkeypatch.setenv(JUDGE_MODEL_ENV, "judge-model")
    assert resolve_judge_model() == "judge-model"


def test_resolve_falls_back_to_main_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ENV, "main-model")
    assert resolve_judge_model() == "main-model"


def test_resolve_explicit_argument_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JUDGE_MODEL_ENV, "judge-model")
    assert resolve_judge_model("explicit") == "explicit"


def test_resolve_ignores_undocumented_judge_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """`JUDGE_MODEL` 是那个写错的变量名，不该被认。"""
    monkeypatch.setenv("JUDGE_MODEL", "wrong-var")
    monkeypatch.setenv(MODEL_ENV, "main-model")
    assert resolve_judge_model() == "main-model"


# ------------------------------------------------------- OpenAIJudge 用同一个源


def test_judge_uses_dedicated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "http://judge.local/v1")
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "k")
    monkeypatch.setenv(MODEL_ENV, "main-model")
    monkeypatch.setenv(JUDGE_MODEL_ENV, "judge-model")
    judge = OpenAIJudge()
    assert judge._model == "judge-model"


# ------------------------------------------------ _factors 与裁判实际模型一致


def test_factors_records_real_judge_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """报告记录的 judge_model 必须就是裁判会用的模型。"""
    from devops_agent.eval.cli import _factors

    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "http://judge.local/v1")
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "k")
    monkeypatch.setenv(MODEL_ENV, "main-model")
    monkeypatch.setenv(JUDGE_MODEL_ENV, "judge-model-x")

    suite = load_suite("eval/suites/devops-core.v1.jsonl")
    factors = _factors(suite, "per-case", judge_enabled=True)
    assert factors["judge_model"] == "judge-model-x"
    assert factors["judge_model"] == OpenAIJudge()._model


def test_factors_clears_judge_model_when_judge_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from devops_agent.eval.cli import _factors

    monkeypatch.setenv(MODEL_ENV, "main-model")
    monkeypatch.setenv(JUDGE_MODEL_ENV, "judge-model")
    suite = load_suite("eval/suites/devops-core.v1.jsonl")
    factors = _factors(suite, "per-case", judge_enabled=False)
    assert factors["judge_model"] == ""
    assert factors["judge_scope"] == ""


def test_changing_judge_model_blocks_comparability(monkeypatch: pytest.MonkeyPatch) -> None:
    """换了裁判模型 -> 不可比，delta 不能被当成结论。"""
    from devops_agent.eval.cli import _factors

    suite = load_suite("eval/suites/devops-core.v1.jsonl")

    def report(pass_rate: float, judge_model: str, scope: str = "all") -> dict[str, object]:
        return {
            "n": 40,
            "pass_rate": pass_rate,
            "hard_failures": {},
            "gates": [],
            "metrics": [],
            "mean_trajectory": 0.9,
            "mean_tokens": 100,
            "p50_latency_ms": 1000,
            "mean_judge": None,
            **{
                **_factors(suite, "per-case", judge_enabled=False),
                "judge_model": judge_model,
                "judge_scope": scope,
            },
        }

    compat = compare(report(0.6, "judge-b"), report(0.9, "judge-a"))["compatibility"]
    assert compat["level"] == "blocking"
    assert "judge_model" in compat["changed"]


def test_changing_judge_scope_blocks_comparability() -> None:
    """裁判覆盖率变了，judge 均值换了分母，两批不可直接比。"""
    base = {
        "n": 40,
        "pass_rate": 0.9,
        "hard_failures": {},
        "gates": [],
        "metrics": [],
        "suite_sha": "s",
        "grader_version": "v2",
        "prompt_sha": "p",
        "model": "m",
        "profile": "per-case",
        "judge_model": "j",
        "judge_scope": "all",
    }
    head = dict(base, judge_scope="passed")
    compat = compare(head, base)["compatibility"]
    assert compat["level"] == "blocking"
    assert "judge_scope" in compat["changed"]


# ------------------------------------------------------- judge scope 无偏默认


def _case(case_id: str, dims: list[str]) -> EvalCase:
    return EvalCase(
        id=case_id,
        tags=["scenario:common"],
        input="q",
        expected={"must_contain": ["ok"]},
        judge=JudgeSpec(rubric="peem.v1", dimensions=dims),
    )


class RecordingJudge:
    """记录被打了多少次分。"""

    def __init__(self, value: float = 4.0) -> None:
        self.calls: list[str] = []
        self._value = value

    def score(self, case: EvalCase, result: TraceResult) -> dict[str, float]:
        self.calls.append(case.id)
        return {dim: self._value for dim in (case.judge.dimensions if case.judge else [])}


def _score(passed: bool) -> CaseScore:
    return CaseScore(
        passed=passed,
        checks=[],
        trajectory_score=0.9 if passed else 0.3,
        hard_failures=[] if passed else ["text"],
    )


def test_default_scope_judges_failed_cases_too() -> None:
    """默认 all：规则失败也要打分，否则退化会让均值反而上升。"""
    judge = RecordingJudge()
    case = _case("c1", ["correctness"])
    trace = TraceResult(status="success", final_text="x")
    _, scored = _score_with_judge(case, trace, _score(False), judge)  # type: ignore[arg-type]
    assert judge.calls == ["c1"]
    assert scored == {"correctness": 4.0}


def test_scope_passed_skips_failed_cases() -> None:
    judge = RecordingJudge()
    case = _case("c1", ["correctness"])
    trace = TraceResult(status="success", final_text="x")
    _, scored = _score_with_judge(
        case,
        trace,
        _score(False),
        judge,
        scope="passed",  # type: ignore[arg-type]
    )
    assert judge.calls == []
    assert scored is None


def test_scope_all_does_not_change_passing_behaviour() -> None:
    judge = RecordingJudge()
    case = _case("c1", ["correctness"])
    trace = TraceResult(status="success", final_text="x")
    for scope in ("all", "passed"):
        judge.calls.clear()
        _, scored = _score_with_judge(
            case,
            trace,
            _score(True),
            judge,
            scope=scope,  # type: ignore[arg-type]
        )
        assert judge.calls == ["c1"]
        assert scored == {"correctness": 4.0}
