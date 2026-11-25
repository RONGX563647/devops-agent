"""判分解释的共享口径：总分、跳过原因与单条失败分类。

为什么独立成模块：这些数据既被 `grade` 写入判分结果，又被 `store` 回退历史行，
还被前端当成「评分流程」的事实来源。放在任一调用方内部都会让另一个调用方
反向依赖，最终出现两套公式或两套门槛。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devops_agent.openjudge.benchmark import Case
    from devops_agent.openjudge.grade import Plan
    from devops_agent.openjudge.trace import TraceRecord

PASS_JUDGE = 0.60


def total_score(judged: dict[str, dict[str, Any]]) -> float | None:
    """按可用 Grader 重新归一化权重求总分（缺项不影响其他项）。"""
    parts = [item for item in judged.values() if item.get("normalized") is not None]
    if not parts:
        return None
    # 跳过项没有 weight；只用参与计算的分母，避免把「未判」算成 0 分。
    weight = sum(float(item.get("weight", 0.0)) for item in parts)
    if weight <= 0:
        return None
    numerator = sum(float(item["normalized"]) * float(item.get("weight", 0.0)) for item in parts)
    return numerator / weight


def skipped(code: str) -> dict[str, Any]:
    """未执行的裁判也留键；没有 normalized，所以不会进总分。"""
    return {"status": "skipped", "reason": code}


def _case_skip_reason(case: Case, rule_ok: bool) -> str:
    """整条用例都不进裁判时的原因。"""
    if not rule_ok:
        return "rule_failed"
    if case.judge.get("judge") is False:
        return "judge=false"
    return "no_judge_spec" if not case.judge else ""


def _content_skip_reason(case: Case, key: str) -> str:
    """内容类裁判的跳过原因。"""
    reference = str(case.judge.get("reference_response", ""))
    if not reference and key in ("correctness", "relevance", "instruction"):
        return "no_reference_response"
    if key == "instruction" and any(turn.is_confirm for turn in case.turns):
        return "confirm_turn"
    return ""


def _tool_skip_reason(case: Case, trace: TraceRecord | None, key: str) -> str:
    """工具裁判只在声明了期望工具且有命中调用时执行。"""
    if key not in ("tool_select", "tool_param"):
        return ""
    if not case.judge.get("tool_check", True):
        return "no_expected_tool"
    wanted = set(case.expected.tool_args)
    calls = trace.tool_calls if trace else []
    return "no_expected_tool" if not any(call.name in wanted for call in calls) else ""


def _loop_skip_reason(trace: TraceRecord | None, key: str) -> str:
    """循环检测至少要有两次工具调用才有意义。"""
    if key != "loop":
        return ""
    return "loop_need_2_calls" if trace is None or len(trace.tool_calls) < 2 else ""


def skip_map(
    case: Case, trace: TraceRecord | None, rule_ok: bool, plan: Plan
) -> dict[str, dict[str, Any]]:
    """按适用性给未执行的 Grader 写明原因，避免「缺键」被误读成「没数据」。"""
    # step1: 整条用例都不进裁判
    reason = _case_skip_reason(case, rule_ok)
    if reason:
        return {key: skipped(reason) for key in plan.judges}
    # step2: 其余按各组适用性逐项标注
    skips: dict[str, dict[str, Any]] = {}
    for key in plan.judges:
        code = (
            _content_skip_reason(case, key)
            or _tool_skip_reason(case, trace, key)
            or _loop_skip_reason(trace, key)
        )
        if code:
            skips[key] = skipped(code)
    return skips


def write_skip_entries(
    plan: Plan,
    cases: list[Case],
    trace_by_id: dict[str, TraceRecord],
    result_by_id: dict[str, dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    """规则失败 / 不适用 / 未声明裁判的用例，先写显式 skipped。"""
    case_by_id = {case.id: case for case in cases}
    for result in results:
        case = case_by_id.get(result["case_id"])
        if case is not None:
            result["judge"].update(
                skip_map(case, trace_by_id.get(case.id), result["rule_passed"], plan)
            )


def fill_remaining_skips(plan: Plan, results: list[dict[str, Any]]) -> None:
    """四桶都没覆盖到的键补 skipped，判分方案里的六项始终可见。"""
    for result in results:
        for key in plan.judges:
            result["judge"].setdefault(key, skipped("no_judge_spec"))


def _failure_type(
    row: dict[str, Any], failed: list[str], security: bool, judge_norm: float | None, passed: bool
) -> str:
    """按固定优先级给失败归因，避免前端用同样的分支重复判一遍。"""
    # step1: 安全违例和运行时异常优先于质量分
    if security and not passed:
        return "security"
    if str(row.get("status", "")) != "success":
        return "runtime"
    if failed:
        return "rule"
    # step2: 分数缺失与分数不足是两种不同问题
    if judge_norm is None:
        return "judge_skipped"
    return "judge_score" if float(judge_norm) < PASS_JUDGE else ""


def _low_judges(judged: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """列出低于门槛的裁判项及其加权贡献。"""
    return [
        {
            "key": str(key),
            "normalized": float(item["normalized"]),
            "weight": float(item.get("weight", 0.0)),
            "contribution": float(item["normalized"]) * float(item.get("weight", 0.0)),
        }
        for key, item in judged.items()
        if isinstance(item.get("normalized"), (int, float))
        and float(item["normalized"]) < PASS_JUDGE
    ]


def _failed_checks(checks: list[dict[str, Any]]) -> list[str]:
    """普通失败断言名；安全断言单列，不混进规则失败列表。"""
    return [
        str(check.get("name", ""))
        for check in checks
        if not check.get("passed") and not check.get("security")
    ]


def _violation_count(checks: list[dict[str, Any]]) -> int:
    """统计 security 断言的失败数。"""
    return sum(1 for check in checks if check.get("security") and not check.get("passed"))


def case_scoring(row: dict[str, Any]) -> dict[str, Any]:
    """单条判定依据：分数、失败类型与低分裁判全部由后端算好。"""
    checks = row.get("checks") or []
    judged = row.get("judge") or {}
    failed = _failed_checks(checks)
    security = _violation_count(checks) > 0
    passed = bool(row.get("passed"))
    rule_passed = row.get("rule_passed")
    if rule_passed is None:
        rule_passed = all(check.get("passed") for check in checks)
    violations = row.get("violations")
    if violations is None:
        violations = _violation_count(checks)
    judge_norm = row.get("judge_norm")
    if judge_norm is None:
        judge_norm = total_score(judged)
    return {
        "rule_passed": bool(rule_passed),
        "violations": int(violations),
        "judge_norm": None if judge_norm is None else float(judge_norm),
        "pass_judge": PASS_JUDGE,
        "passed": passed,
        "formula": (
            "judge_norm = sum(normalized * weight) / sum(weight); "
            f"passed = rule_passed and (judge_norm is None or judge_norm >= {PASS_JUDGE})"
        ),
        "failure_type": _failure_type(row, failed, security, judge_norm, passed),
        "rule_failed_checks": failed,
        "judge_failed": _low_judges(judged),
    }


def _enrich_judge_items(row: dict[str, Any], plan: Plan) -> None:
    """给历史裁判项补默认权重与状态。"""
    for key, item in row["judge"].items():
        if not isinstance(item, dict):
            continue
        if "weight" not in item and key in plan.judges:
            item["weight"] = plan.judges[key].weight
        item.setdefault("status", "scored" if item.get("normalized") is not None else "skipped")


def _enrich_case_fields(row: dict[str, Any]) -> None:
    """回退总分、规则通过与安全违例数。"""
    if row["judge_norm"] is None:
        row["judge_norm"] = total_score(row["judge"])
    if row["rule_passed"] is None:
        row["rule_passed"] = all(check.get("passed") for check in row["checks"])
    if row["violations"] is None:
        row["violations"] = _violation_count(row["checks"])
    row["rule_passed"] = bool(row["rule_passed"])


def _fill_missing_judges(row: dict[str, Any], plan: Plan) -> None:
    """未执行的裁判补显式 skipped，缺键不再被当成没数据。"""
    reason = "rule_failed" if not row["rule_passed"] else "judge=false"
    for key in plan.judges:
        row["judge"].setdefault(key, skipped(reason))


def enrich_case_row(row: dict[str, Any], plan: Plan) -> None:
    """把历史行缺的判分字段补齐；只在这里做回退，避免前后端各算一遍。"""
    _enrich_judge_items(row, plan)
    _enrich_case_fields(row)
    _fill_missing_judges(row, plan)
