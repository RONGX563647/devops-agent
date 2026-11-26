"""批次指标与失败归因：KPI、多次试验口径、失败清单与过程指标。

数据源只有两份：`results`（每条用例的判定）与 `traces`（执行轨迹）。
所有数字都在这里一次算清，报告层不再二次加工 —— 同一个数在页面上
出现两个值时，一定是有两处算法。
"""

from __future__ import annotations

import math
import os
from typing import Any

from devops_agent.openjudge import explain, stats
from devops_agent.openjudge.gates import track_stats
from devops_agent.openjudge.trace import TraceRecord

MAX_FAILURES = 50
MAX_PARETO = 20


def p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def failure_type(result: dict[str, Any], judge_dims: list[str]) -> str:
    """单条失败的归类：规则 > 运行异常 > 裁判低分 > 缺裁判。

    顺序即优先级：规则失败时裁判根本没跑（见 grade._finish），
    把它标成「裁判低分」会让人去调裁判，而真正要改的是断言。
    """
    if result.get("rule_passed") is False:
        return "rule"
    if str(result.get("status", "")) not in ("", "success", "parked"):
        return "runtime"
    if judge_dims:
        return "judge_score"
    if result.get("judge_missing"):
        return "judge_missing"
    return "unknown"


def judge_dims(result: dict[str, Any]) -> list[str]:
    """低于单条门槛的裁判维度：这是「裁判判负」能落到原因上的唯一依据。"""
    dims: list[str] = []
    for key, item in (result.get("judge") or {}).items():
        if not isinstance(item, dict):
            continue
        normalized = item.get("normalized")
        if isinstance(normalized, (int, float)) and float(normalized) < explain.PASS_JUDGE:
            dims.append(str(key))
    return dims


def failures_of(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], int]:
    """所有未通过的用例：规则失败与裁判判负都要给出原因，不只统计规则。"""
    failures: list[dict[str, Any]] = []
    rule_pareto: dict[str, int] = {}
    judge_pareto: dict[str, int] = {}
    for result in results:
        if result.get("passed"):
            continue
        checks = result.get("checks") or []
        names = [str(check["name"]) for check in checks if not check.get("passed")]
        dims = judge_dims(result)
        for name in names:
            rule_pareto[name] = rule_pareto.get(name, 0) + 1
        for key in dims:
            judge_pareto[key] = judge_pareto.get(key, 0) + 1
        failures.append(
            {
                "case_id": result.get("case_id", ""),
                "track": result.get("track", ""),
                "status": result.get("status", ""),
                "failed_checks": names,
                "judge_dims": dims,
                "failure_type": failure_type(result, dims),
                "repro": f"python -m devops_agent.openjudge run --case {result.get('case_id', '')}",
            }
        )
    pareto = {
        "rule": [
            {"check": name, "count": count}
            for name, count in sorted(rule_pareto.items(), key=lambda item: (-item[1], item[0]))
        ],
        "judge": [
            {"check": name, "count": count}
            for name, count in sorted(judge_pareto.items(), key=lambda item: (-item[1], item[0]))
        ],
    }
    return failures, pareto, len(failures)


def subject_stats(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    subject: dict[str, dict[str, int]] = {}
    for result in results:
        key = str(result.get("subject", "") or "unknown")
        bucket = subject.setdefault(key, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if result.get("passed") else 0
    return subject


def _token_totals(traces: list[TraceRecord]) -> dict[str, int]:
    return {
        "input": sum(trace.tokens.get("input", 0) for trace in traces),
        "output": sum(trace.tokens.get("output", 0) for trace in traces),
        "cache_hit": sum(trace.tokens.get("cache_hit", 0) for trace in traces),
    }


def _failure_counts(
    results: list[dict[str, Any]],
) -> tuple[dict[str, int], int, int, int]:
    """终态分布 + 规则失败 / 裁判低分 / 未判分三类计数。"""
    status_counts: dict[str, int] = {}
    rule_failed = judge_failed = not_judged = 0
    for item in results:
        status = str(item.get("status", "") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        if not item.get("rule_passed"):
            rule_failed += 1
        judge = item.get("judge_norm")
        if judge is None:
            not_judged += 1
        elif float(judge) < explain.PASS_JUDGE:
            judge_failed += 1
    return status_counts, rule_failed, judge_failed, not_judged


def _case_groups(results: list[dict[str, Any]]) -> dict[str, list[bool]]:
    groups: dict[str, list[bool]] = {}
    for result in results:
        groups.setdefault(str(result.get("case_id", "")), []).append(bool(result.get("passed")))
    return groups


def _cost(tokens: dict[str, int]) -> tuple[float | None, str]:
    """没配单价就返回 None，不写 0.0 冒充「免费」。"""
    raw = os.environ.get("DEVOPS_AGENT_OPENJUDGE_USD_PER_1K", "").strip()
    try:
        price = float(raw) if raw else 0.0
    except ValueError:
        price = 0.0
    if price <= 0:
        return None, "未配置单价（DEVOPS_AGENT_OPENJUDGE_USD_PER_1K）"
    total = tokens.get("input", 0) + tokens.get("output", 0)
    return round(total / 1000 * price, 4), ""


def _k_totals(groups: dict[str, list[bool]]) -> tuple[int, int]:
    """pass@k 的分子与 pass^k 的分子；分母都是去重后的用例条数。"""
    at_k = sum(1 for marks in groups.values() if any(marks))
    hat_k = sum(1 for marks in groups.values() if marks and all(marks))
    return at_k, hat_k


def _trials(results: list[dict[str, Any]]) -> int:
    return max((int(item.get("trial", 0)) for item in results), default=0) + 1


def _unvalidated(results: list[dict[str, Any]]) -> int:
    return sum(1 for item in results if item.get("outcome") == "unvalidated")


def kpi_of(results: list[dict[str, Any]], traces: list[TraceRecord]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for item in results if item.get("passed"))
    status_counts, rule_failed, judge_failed, not_judged = _failure_counts(results)
    groups = _case_groups(results)
    case_count = len(groups)
    at_k, hat_k = _k_totals(groups)
    trials = _trials(results)
    tokens = _token_totals(traces)
    cost, cost_note = _cost(tokens)
    return {
        "cases": total,
        "case_count": case_count,
        "runs": total,
        "trials": trials,
        "passed": passed,
        "pass_rate": passed / total if total else 0.0,
        # pass@k 是稳定性上界（至少过一次），pass^k 是下界（k 次全过）；
        # k=1 时两者相等，前端据此决定要不要多显示一列。
        "pass_at_k": at_k / case_count if case_count else 0.0,
        "pass_hat_k": hat_k / case_count if case_count else 0.0,
        "ci": stats.wilson_interval(at_k, case_count),
        "violations": sum(int(result.get("violations", 0)) for result in results),
        "unvalidated": _unvalidated(results),
        "judge_n": total - not_judged,
        "latency_p95_ms": p95([trace.latency_ms for trace in traces]),
        "tokens": tokens,
        "cost_usd": cost,
        "cost_note": cost_note,
        "status_counts": status_counts,
        "rule_failed_cases": rule_failed,
        "judge_failed_cases": judge_failed,
        "not_judged_cases": not_judged,
    }


_FAIL_STATE_TOKENS = ("error", "fail", "denied", "reject", "cancel", "timeout")


def _call_ok(call: Any) -> bool:
    state = str(getattr(call, "state", "") or "").lower()
    if not state:
        return True
    return not any(token in state for token in _FAIL_STATE_TOKENS)


def _has_retry(trace: TraceRecord) -> bool:
    seen: set[str] = set()
    for call in trace.tool_calls:
        signature = f"{call.name}:{sorted(call.args.items())}"
        if signature in seen:
            return True
        seen.add(signature)
    return False


def _ratio(marks: list[bool]) -> float | None:
    """没有样本时给 None：0% 和「没算过」在图表上是两件事。"""
    return sum(1 for ok in marks if ok) / len(marks) if marks else None


def _rate(items: list[TraceRecord], pred: Any) -> float:
    return sum(1 for item in items if pred(item)) / len(items)


def _has_confirm(trace: TraceRecord) -> bool:
    return "confirm" in " ".join(trace.events).lower()


def _param_marks(results: list[dict[str, Any]]) -> dict[str, list[bool]]:
    """工具入参质量只能由裁判给出，按赛道把已判分的 tool_param 汇到一起。"""
    marks: dict[str, list[bool]] = {}
    for result in results:
        entry = (result.get("judge") or {}).get("tool_param") or {}
        normalized = entry.get("normalized") if isinstance(entry, dict) else None
        if isinstance(normalized, (int, float)):
            ok = float(normalized) >= explain.PASS_JUDGE
            marks.setdefault(str(result.get("track", "") or "?"), []).append(ok)
    return marks


def _stage_of(items: list[TraceRecord], marks: list[bool]) -> dict[str, Any]:
    calls = [call for trace in items for call in trace.tool_calls]
    steps = [len(trace.tool_calls) for trace in items]
    return {
        "traces": len(items),
        "tool_calls": len(calls),
        "tool_call_success_rate": (
            sum(1 for call in calls if _call_ok(call)) / len(calls) if calls else None
        ),
        "tool_param_ok_rate": _ratio(marks),
        "avg_steps": round(sum(steps) / len(steps), 2) if steps else 0.0,
        "retry_rate": _rate(items, _has_retry),
        "timeout_rate": _rate(items, lambda trace: trace.status == "timeout"),
        "confirm_rate": _rate(items, _has_confirm),
    }


def process_stages(
    traces: list[TraceRecord], results: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """过程指标：白皮书的「过程评测集」，按赛道下钻，数据源就是已有 trace。"""
    by_track: dict[str, list[TraceRecord]] = {}
    for trace in traces:
        by_track.setdefault(trace.track or "?", []).append(trace)
    param = _param_marks(results)
    return {track: _stage_of(items, param.get(track, [])) for track, items in by_track.items()}


def challenge_tracks(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not results:
        return {}
    return track_stats(results)


#: 与 `tools/names.SEARCH_TOOL_NAME` 同值：openjudge 是纯数据层，不为一个字符串
#: 引入对 tools 包的依赖；`test_web_search_tool.py` 断言两者一致，改名会立刻红。
SEARCH_TOOL = "web_search"


def search_stats(traces: list[TraceRecord]) -> dict[str, Any]:
    """检索工具的调用统计，从已有 `tool_calls` 投影，不额外采样。

    这是工具化相对「供应商侧 RAG」最直接的收益：检索从黑盒变成轨迹里的
    `name + args.query + result_excerpt`，判分与成本核算都能消费。
    判定依据是 `tools/web_search.render()` 的首行契约（`hits=` / `note=error=`
    / `skipped=budget-exhausted`），两侧一起改才不失效。
    """
    calls = 0
    empty = 0
    failed = 0
    exhausted = 0
    cases: set[str] = set()
    queries: list[str] = []
    for trace in traces:
        for call in trace.tool_calls:
            if call.name != SEARCH_TOOL:
                continue
            calls += 1
            cases.add(trace.case_id)
            queries.append(str(call.args.get("query", ""))[:80])
            excerpt = call.result_excerpt
            if "hits=0" in excerpt:
                empty += 1
            if "note=error=" in excerpt:
                failed += 1
            if "budget-exhausted" in excerpt:
                exhausted += 1
    return {
        "calls": calls,
        "empty": empty,
        "failed": failed,
        "budget_exhausted": exhausted,
        "cases": sorted(cases),
        "queries": queries[:20],
    }
