"""前缀缓存可观测性的单测。

锁一个已证实的缺口：`profile_note.py` 里那个「21% → 96%」的收益，
在 report.json / report.md / cases.jsonl 里都没有位置——因为 `_add_tokens`
只累加 input/output，而缓存量被丢掉了。提示词布局改坏时，分数可能不动，
唯一的信号就是缓存命中率。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from devops_agent.eval.dataset import EvalCase
from devops_agent.eval.graders import CaseScore, TraceResult
from devops_agent.eval.report import aggregate, render_markdown
from devops_agent.eval.runner import (
    CaseRun,
    _add_cache_tokens,
    _empty_state,
    case_run_payload,
    execute_case,
)

# ----------------------------------------------------------- 事件解析


def test_add_cache_tokens_reads_both_fields() -> None:
    cache_in, cache_new = _add_cache_tokens(
        {"cache_input_tokens": 900, "cache_creation_input_tokens": 1200}
    )
    assert (cache_in, cache_new) == (900, 1200)


def test_add_cache_tokens_defaults_to_zero() -> None:
    """字段缺失（模型不支持）按 0 处理，不报错。"""
    assert _add_cache_tokens({}) == (0, 0)


def test_add_cache_tokens_rejects_non_int() -> None:
    assert _add_cache_tokens(
        {"cache_input_tokens": "900", "cache_creation_input_tokens": None}
    ) == (
        0,
        0,
    )


# ----------------------------------------------------------- 事件流累计


class FakeRuntime:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def stream(self, session_id: str, user_input: str):  # noqa: ANN201
        for event in self._events:
            yield event


def _case() -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": "cache-1",
            "tags": ["scenario:common"],
            "input": "x",
            "expected": {"status": "success", "must_contain": ["ok"]},
        }
    )


def test_runner_accumulates_cache_tokens(tmp_path: Path) -> None:
    """两条 MODEL_CALL_END 的缓存量要累加进 trace。"""
    events = [
        {"type": "TEXT_BLOCK_DELTA", "delta": "ok"},
        {
            "type": "MODEL_CALL_END",
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_input_tokens": 800,
            "cache_creation_input_tokens": 200,
        },
        {
            "type": "MODEL_CALL_END",
            "input_tokens": 500,
            "output_tokens": 50,
            "cache_input_tokens": 400,
            "cache_creation_input_tokens": 0,
        },
    ]
    _score, trace, _judge = asyncio.run(execute_case(_case(), lambda _c, _w: FakeRuntime(events)))
    assert trace.cache_input_tokens == 1200
    assert trace.cache_creation_tokens == 200
    assert trace.tokens == 1650  # 1100 + 550


def test_runner_payload_carries_cache_fields() -> None:
    """cases.jsonl 要能复盘「这一批缓存有没有命中」。"""
    trace = TraceResult(
        status="success",
        final_text="ok",
        cache_input_tokens=900,
        cache_creation_tokens=100,
        tokens=1000,
    )
    run = CaseRun(
        case=_case(),
        score=CaseScore(passed=True, checks=[], trajectory_score=1.0),
        trace=trace,
        judge_scores=None,
        latency_ms=10,
        tokens=1000,
    )
    payload = case_run_payload(run)
    assert payload["cache_input_tokens"] == 900
    assert payload["cache_creation_tokens"] == 100


# ----------------------------------------------------------- 聚合与渲染


def _run(cache_in: int, cache_new: int, tokens: int) -> CaseRun:
    trace = TraceResult(
        status="success",
        final_text="ok",
        tokens=tokens,
        cache_input_tokens=cache_in,
        cache_creation_tokens=cache_new,
    )
    return CaseRun(
        case=_case(),
        score=CaseScore(passed=True, checks=[], trajectory_score=1.0),
        trace=trace,
        judge_scores=None,
        latency_ms=10,
        tokens=tokens,
    )


def test_aggregate_computes_cache_hit_rate() -> None:
    runs = [_run(800, 200, 1000), _run(400, 0, 500)]
    summary = aggregate(runs, min_cases_for_gate=1)
    assert summary["cache_input"] == 1200
    assert summary["cache_creation"] == 200
    assert summary["cache_hit_rate"] == round(1200 / 1500, 4)


def test_aggregate_cache_hit_rate_none_without_data() -> None:
    """模型不支持缓存时命中的是 0 除以 0，应给 None 而不是 0（0 会被读成「完全没命中」）。"""
    summary = aggregate([_run(0, 0, 0)], min_cases_for_gate=1)
    assert summary["cache_hit_rate"] is None


def test_render_reports_cache_hit_rate() -> None:
    summary = aggregate([_run(960, 40, 1000)], min_cases_for_gate=1)
    md = render_markdown(summary)
    assert "前缀缓存命中 96.0%" in md


def test_render_marks_cache_absent() -> None:
    """没有数据时显式写「无数据」，不要静默省略。"""
    summary = aggregate([_run(0, 0, 0)], min_cases_for_gate=1)
    md = render_markdown(summary)
    assert "前缀缓存：无数据" in md


def test_cache_rate_detects_layout_regression() -> None:
    """核心场景：同一批题库，提示词布局改坏后命中率从 96% 掉到 21%。

    这是本 PR 存在的理由 —— 通过率完全相同的两批，能靠缓存率区分出来。
    """
    good = aggregate([_run(960, 40, 1000)], min_cases_for_gate=1)
    bad = aggregate([_run(210, 790, 1000)], min_cases_for_gate=1)
    assert good["pass_rate"] == bad["pass_rate"] == 1.0
    assert good["cache_hit_rate"] == 0.96
    assert bad["cache_hit_rate"] == 0.21


# ----------------------------------------------------------- state 初始化


def test_empty_state_has_cache_fields() -> None:
    state = _empty_state()
    assert state["cache_input_tokens"] == 0
    assert state["cache_creation_tokens"] == 0
