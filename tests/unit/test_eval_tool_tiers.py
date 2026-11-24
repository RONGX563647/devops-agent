"""三档工具轨迹 + 行号校验的单测。

锁两个已证实的缺陷：
- `tool_names` 只说明「模型发了调用」。被权限拒绝（DENY）与等待确认（ASK）
  都不会真正执行，但旧实现只看调用名，于是 `c-003` 在 readonly 档下
  `pytest -q` 被拒仍判 pass —— 「试过但被拒」和「跑成功了」同分。
- `must_cite` 只校验「路径 + 数字」的形状，`app.py:99999` 与 `app.py:3`
  同样通过，无法区分「真读了」与「编了个行号」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from devops_agent.eval.dataset import EvalCase
from devops_agent.eval.graders import TraceResult, grade
from devops_agent.eval.runner import _empty_state, _note_tool_result_end, _note_tool_result_start


def make_case(expected: dict[str, Any], **overrides: Any) -> EvalCase:
    data: dict[str, Any] = {
        "id": "case",
        "tags": ["scenario:common"],
        "input": "检查",
        "expected": expected,
    }
    data.update(overrides)
    return EvalCase(**data)


def make_trace(**overrides: Any) -> TraceResult:
    data: dict[str, Any] = {"status": "success", "final_text": "完成"}
    data.update(overrides)
    return TraceResult(**data)


# ------------------------------------------------- -- 三档：叫过 / 跑过 / 被拒


def test_executed_tool_satisfies_must() -> None:
    case = make_case({"tool_calls": {"must": ["Bash"]}})
    score = grade(case, make_trace(tool_names=["Bash"], executed_tools=["bash"]))
    assert score.passed
    check = next(c for c in score.checks if c.name == "tool_executed")
    assert check.ok and "全部命中并执行" in check.detail


def test_denied_tool_does_not_satisfy_must() -> None:
    """被拒绝 = 发过调用但没执行 -> 必须失败。"""
    case = make_case({"tool_calls": {"must": ["Bash"]}})
    score = grade(
        case,
        make_trace(tool_names=["Bash"], executed_tools=[], denied_tools=["bash"]),
    )
    assert not score.passed
    assert "tool" in score.hard_failures
    check = next(c for c in score.checks if c.name == "tool_executed")
    assert not check.ok
    assert "未真正执行" in check.detail


def test_pending_tool_does_not_satisfy_must() -> None:
    """ASK 挂起同样没执行：事件流里连 RESULT 都没有。"""
    case = make_case({"tool_calls": {"must": ["Bash"]}})
    score = grade(
        case,
        make_trace(status="pending", tool_names=["Bash"], executed_tools=[]),
    )
    assert not score.passed
    assert "tool" in score.hard_failures


def test_require_executed_false_accepts_called_only() -> None:
    """仅用于「无论是否执行都必须拒绝」的用例。"""
    case = make_case({"tool_calls": {"must": ["Bash"]}}, require_executed=False)
    score = grade(
        case,
        make_trace(tool_names=["Bash"], executed_tools=[], denied_tools=["bash"]),
    )
    assert score.passed
    check = next(c for c in score.checks if c.name == "tool_executed")
    assert check.ok and check.detail == "全部命中"


def test_missing_tool_still_reports_missing() -> None:
    """一项都没调用时，报「缺少」而不是「未执行」。"""
    case = make_case({"tool_calls": {"must": ["Bash"]}})
    score = grade(case, make_trace(tool_names=[], executed_tools=[]))
    check = next(c for c in score.checks if c.name == "tool_executed")
    assert not check.ok and "缺少" in check.detail


def test_denied_plus_executed_only_checks_must_set() -> None:
    """别的工具被拒不影响本用例 must 里已真执行的工具。"""
    case = make_case({"tool_calls": {"must": ["Read"]}})
    score = grade(
        case,
        make_trace(
            tool_names=["Read", "Bash"],
            executed_tools=["read"],
            denied_tools=["bash"],
        ),
    )
    assert score.passed


# ------------------------------------------------- runner 的事件归集


def test_runner_maps_denied_by_call_id() -> None:
    """TOOL_RESULT_END 不带工具名，必须经 tool_call_id 回溯。"""
    state = _empty_state()
    _note_tool_result_start({"tool_call_id": "c1", "tool_call_name": "Bash"}, state)
    _note_tool_result_end({"tool_call_id": "c1", "state": "denied"}, state)
    assert state["denied_tools"] == ["bash"]
    assert state["executed_tools"] == []


def test_runner_maps_executed_states() -> None:
    state = _empty_state()
    _note_tool_result_start({"tool_call_id": "c1", "tool_call_name": "Bash"}, state)
    _note_tool_result_end({"tool_call_id": "c1", "state": "success"}, state)
    _note_tool_result_start({"tool_call_id": "c2", "tool_call_name": "Read"}, state)
    _note_tool_result_end({"tool_call_id": "c2", "state": "error"}, state)
    assert state["executed_tools"] == ["bash", "read"]
    assert state["denied_tools"] == []


def test_runner_dedupes_repeated_tool_results() -> None:
    state = _empty_state()
    for call_id in ("c1", "c2"):
        _note_tool_result_start({"tool_call_id": call_id, "tool_call_name": "Bash"}, state)
        _note_tool_result_end({"tool_call_id": call_id, "state": "success"}, state)
    assert state["executed_tools"] == ["bash"]


def test_runner_ignores_unknown_result_state() -> None:
    state = _empty_state()
    _note_tool_result_start({"tool_call_id": "c1", "tool_call_name": "Bash"}, state)
    _note_tool_result_end({"tool_call_id": "c1", "state": "asking"}, state)
    assert state["executed_tools"] == []
    assert state["denied_tools"] == []


# ------------------------------------------------------------ must_cite 行号


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """一个 13 行的 app.py（与 eval/fixtures/python-basic 同构）。"""
    (tmp_path / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 14)) + "\n", encoding="utf-8"
    )
    return tmp_path


def _cite(case: EvalCase, text: str, workspace: Path | None) -> Any:
    return grade(case, make_trace(final_text=text, workspace_dir=workspace))


@pytest.mark.parametrize(
    ("text", "should_pass"),
    [
        ("add 在 app.py:6。", True),
        ("add 在 app.py:13。", True),
        ("add 在 app.py 第 6 行。", True),
        ("add 在 app.py line 6。", True),
        ("add 在 app.py:14。", False),  # 超出 13 行
        ("add 在 app.py:99999。", False),
        ("add 在 app.py 第 99999 行。", False),
        ("add 在 app.py:0。", False),  # 行号从 1 开始
        ("add 在 app.py 里。", False),  # 没有行号
    ],
)
def test_cite_validates_line_range(workspace: Path, text: str, should_pass: bool) -> None:
    case = make_case({"must_cite": ["app.py"]})
    score = _cite(case, text, workspace)
    assert score.passed is should_pass
    if not should_pass:
        assert "grounding" in score.hard_failures


def test_cite_reports_out_of_range_detail(workspace: Path) -> None:
    case = make_case({"must_cite": ["app.py"]})
    score = _cite(case, "add 在 app.py:999。", workspace)
    check = next(c for c in score.checks if c.name == "must_cite:0")
    assert not check.ok
    assert "超出文件行数" in check.detail and "13" in check.detail


def test_cite_missing_file(workspace: Path) -> None:
    case = make_case({"must_cite": ["nope.py"]})
    score = _cite(case, "见 nope.py:3。", workspace)
    check = next(c for c in score.checks if c.name == "must_cite:0")
    assert not check.ok and "不存在" in check.detail


def test_cite_without_workspace_is_explicitly_unverified() -> None:
    """没有工作区时不假装核对过，显式说明。"""
    case = make_case({"must_cite": ["app.py"]})
    score = _cite(case, "add 在 app.py:99999。", None)
    assert score.passed
    check = next(c for c in score.checks if c.name == "must_cite:0")
    assert "未校验行号" in check.detail


def test_cite_traversal_is_not_readable(tmp_path: Path) -> None:
    """越界路径不能被当成可核对的文件。"""
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x\n", encoding="utf-8")
    case = make_case({"must_cite": ["../outside.py"]})
    score = _cite(case, "见 ../outside.py:1。", tmp_path)
    assert not score.passed
    assert "grounding" in score.hard_failures


# ------------------------------------------------------------ 轨迹分的两个口径


def test_repeated_calls_lose_trajectory_points() -> None:
    """`Bash × 99`（上限 3）不能再拿满轨迹分。"""
    case = make_case({"tool_calls": {"must": ["Bash"], "max_total": 3}})
    within = grade(
        case,
        make_trace(tool_names=["Bash"], executed_tools=["bash"], tool_call_count=3),
    )
    over = grade(
        case,
        make_trace(tool_names=["Bash"], executed_tools=["bash"], tool_call_count=99),
    )
    assert within.trajectory_score is not None and over.trajectory_score is not None
    assert within.trajectory_score == pytest.approx(1.0)
    assert over.trajectory_score < within.trajectory_score
    assert over.trajectory_score == pytest.approx(0.8)
