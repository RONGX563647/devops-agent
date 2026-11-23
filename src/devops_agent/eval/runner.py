"""评测执行器 —— 在临时工作区里跑一条用例，收集轨迹并判卷。

关键约束：
- 每条用例独立临时目录，fixture 用 copytree 复制，跑完无条件清理；
- 运行时通过 `make_runtime(case, workdir)` 注入，本模块不 import agentscope；
- 事件消费只认稳定 dict 契约，与 runtime/events.py 的输出一致；
- 超时、异常、等待用户确认都折算成 TraceResult.status，不抛给上层。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devops_agent.eval.dataset import EvalCase, Suite
from devops_agent.eval.graders import CaseScore, Check, TraceResult, grade
from devops_agent.eval.judge import Judge

# runner.py -> eval -> devops_agent -> src -> 仓库根
DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "eval" / "fixtures"

# 单条事件的 token 累加只认这两类（模型调用结束与回复结束）。
_TOKEN_EVENTS = frozenset({"MODEL_CALL_END", "REPLY_END"})

RuntimeFactory = Callable[[EvalCase, Path], Any]


@dataclass(frozen=True, slots=True)
class CaseRun:
    """一条用例的完整运行结果。"""

    case: EvalCase
    score: CaseScore
    trace: TraceResult
    judge_scores: dict[str, float] | None
    latency_ms: int
    tokens: int


def _split_tool_names(raw: object) -> list[str]:
    """`tool_call_name` 可能是逗号分隔的多个名字，拆分并去重保序。"""
    if not isinstance(raw, str):
        return []
    seen: set[str] = set()
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _add_tokens(event: dict[str, Any]) -> int:
    """累加事件里的输入/输出 token；非数字按 0 处理。"""
    total = 0
    for key in ("input_tokens", "output_tokens"):
        value = event.get(key, 0)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


async def _consume(
    runtime: Any,
    case: EvalCase,
    state: dict[str, Any],
) -> None:
    """消费事件流并把结果写进 state（避免闭包 nonlocal 满天飞）。"""
    stream: AsyncIterator[dict[str, Any]] = runtime.stream(f"eval-{case.id}", case.input)
    async for event in stream:
        event_type = event.get("type")
        if event_type == "TEXT_BLOCK_DELTA":
            delta = event.get("delta")
            if isinstance(delta, str):
                state["final_text"] += delta
        elif event_type == "TOOL_CALL_START":
            names = _split_tool_names(event.get("tool_call_name"))
            state["tool_call_count"] += len(names)
            for name in names:
                key = name.lower()
                if key not in state["tool_seen"]:
                    state["tool_seen"].add(key)
                    state["tool_names"].append(name)
        elif event_type in _TOKEN_EVENTS:
            state["tokens"] += _add_tokens(event)
        elif event_type == "REQUIRE_USER_CONFIRM":
            state["status"] = "pending"
            state["error"] = "等待用户确认，评测视为未完成"
            return


def _empty_state() -> dict[str, Any]:
    return {
        "final_text": "",
        "tool_names": [],
        "tool_call_count": 0,
        "tool_seen": set(),
        "tokens": 0,
        "status": "success",
        "error": None,
    }


def _score_with_judge(
    case: EvalCase,
    trace: TraceResult,
    score: CaseScore,
    judge: Judge | None,
) -> tuple[CaseScore, dict[str, float] | None]:
    """规则判卷通过后才调用裁判；裁判故障降级为 None，不否定规则结论。"""
    if judge is None or case.judge is None or not score.passed:
        return score, None
    try:
        judged = judge.score(case, trace)
    except Exception as exc:  # 裁判是外部服务，任何故障都不能拖垮整条用例
        checks = [*score.checks, Check(name="judge", ok=False, detail=str(exc))]
        degraded = CaseScore(
            passed=score.passed,
            checks=checks,
            trajectory_score=score.trajectory_score,
            hard_failures=score.hard_failures,
        )
        return degraded, None
    checks = [
        *score.checks,
        Check(name="judge", ok=True, detail=", ".join(f"{k}={v:g}" for k, v in judged.items())),
    ]
    return CaseScore(
        passed=score.passed,
        checks=checks,
        trajectory_score=score.trajectory_score,
        hard_failures=score.hard_failures,
    ), judged


async def execute_case(
    case: EvalCase,
    make_runtime: RuntimeFactory,
    fixture_root: str | Path | None = None,
    judge: Judge | None = None,
) -> tuple[CaseScore, TraceResult, dict[str, float] | None]:
    """执行一条用例并返回 (规则分数, 轨迹, judge 分数)。"""
    root = Path(fixture_root) if fixture_root is not None else DEFAULT_FIXTURE_ROOT
    workdir = Path(tempfile.mkdtemp(prefix=f"eval-{case.id}-"))
    state = _empty_state()
    started = time.monotonic()
    try:
        if case.fixture:
            source = root / case.fixture
            try:
                shutil.copytree(source, workdir, dirs_exist_ok=True)
            except OSError as exc:
                state["status"] = "error"
                state["error"] = f"fixture 复制失败: {exc}"
        if state["status"] == "success":
            try:
                runtime = make_runtime(case, workdir)
                await asyncio.wait_for(_consume(runtime, case, state), timeout=case.timeout_s)
            except TimeoutError:
                state["status"] = "error"
                state["error"] = f"超时（{case.timeout_s}s）"
            except Exception as exc:  # 运行时异常同样是评测结论的一部分
                state["status"] = "error"
                state["error"] = str(exc)
        latency_ms = int((time.monotonic() - started) * 1000)
        trace = TraceResult(
            status=str(state["status"]),
            final_text=str(state["final_text"]),
            tool_names=list(state["tool_names"]),
            tool_call_count=int(state["tool_call_count"]),
            tokens=int(state["tokens"]),
            latency_ms=latency_ms,
            workspace_dir=workdir,
            error=state["error"] if isinstance(state["error"], str) else None,
        )
        score = grade(case, trace)
        score, judge_scores = _score_with_judge(case, trace, score, judge)
        return score, trace, judge_scores
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def run_suite(
    suite: Suite,
    make_runtime: RuntimeFactory,
    fixture_root: str | Path | None = None,
    judge: Judge | None = None,
    limit: int | None = None,
) -> list[CaseRun]:
    """顺序执行用例集（限制并发，避免评测把真实模型配额打满）。"""
    cases = suite.cases[:limit] if limit is not None else suite.cases
    runs: list[CaseRun] = []
    for case in cases:
        score, trace, judge_scores = await execute_case(
            case, make_runtime, fixture_root=fixture_root, judge=judge
        )
        runs.append(
            CaseRun(
                case=case,
                score=score,
                trace=trace,
                judge_scores=judge_scores,
                latency_ms=trace.latency_ms,
                tokens=trace.tokens,
            )
        )
    return runs


def case_run_payload(run: CaseRun) -> dict[str, Any]:
    """把 CaseRun 摊平成 JSON 友好结构（写 cases.jsonl / DB 用）。"""
    return {
        "case_id": run.case.id,
        "tags": list(run.case.tags),
        "status": run.trace.status,
        "passed": run.score.passed,
        "hard_failures": list(run.score.hard_failures),
        "trajectory_score": run.score.trajectory_score,
        "judge": run.judge_scores,
        "latency_ms": run.latency_ms,
        "tokens": run.tokens,
        "error": run.trace.error,
        "checks": [
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in run.score.checks
        ],
    }
