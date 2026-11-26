"""判分：规则门禁 + OpenJudge 语义裁判 + 归一化加权。

分工：
- 规则门禁（rulegate）先判安全/事实/入参，失败轨迹**不进裁判**；
- 语义质量交给 OpenJudge Grader（correctness / relevance / instruction /
  tool_select / tool_param / loop），只处理规则通过的部分；
- 不同 Grader 量纲不同（1–5 与 0–1），统一归一化后再加权，避免「1 分制的
  异常检测」和「5 分制的正确性」直接相加。

模型凭据只从现有网关读取（`DEVOPS_AGENT_*`），缺变量时抛 ConfigError 由 CLI
退出 2 —— 不能把「没配模型」记成「模型答得差」。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from devops_agent.openjudge.benchmark import Case
from devops_agent.openjudge.explain import (
    PASS_JUDGE,
    fill_remaining_skips,
    total_score,
    write_skip_entries,
)
from devops_agent.openjudge.judge_model import judge_model as judge_model
from devops_agent.openjudge.rubric_gate import frozen_rubric_items as _frozen_rubric_items
from devops_agent.openjudge.rubric_gate import rubric_gate as rubric_gate
from devops_agent.openjudge.rubric_gate import rubric_sha as rubric_sha
from devops_agent.openjudge.rubric_gate import rubric_template as _rubric_template
from devops_agent.openjudge.rulegate import rule_passed, run_checks, violations
from devops_agent.openjudge.trace import TraceRecord

PLAN_PATH = Path(__file__).resolve().parents[3] / "eval" / "openjudge" / "plan.yaml"

# 工具 schema 的唯一事实来源（ToolSelection / ToolParameter 裁判需要）。
# 必须与 agentscope 内置工具的真实入参一致：早先手工精简成 4 个参数，
# 结果每次 Grep 都因为 output_mode/-A/context 被判「参数不合法」，
# tool_param 恒为 0，把「裁判看不懂工具」记成了「模型用错工具」。
# 真实 schema 见 agentscope.tool.(Bash|Read|Glob|Grep|Write|Edit).input_schema。
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "Bash",
        "description": "在用例工作区执行 shell 命令",
        "parameters": {
            "command": "str",
            "description": "str",
            "timeout": "int",
        },
    },
    {
        "name": "Read",
        "description": "读取文件内容",
        "parameters": {
            "file_path": "str",
            "offset": "int",
            "limit": "int",
            "pages": "str|null",
        },
    },
    {
        "name": "Glob",
        "description": "按文件名模式查找文件",
        "parameters": {
            "pattern": "str",
            "path": "str",
            "head_limit": "int",
            "offset": "int",
        },
    },
    {
        "name": "Grep",
        "description": "按正则搜索文件内容（ripgrep）",
        "parameters": {
            "pattern": "str",
            "path": "str",
            "output_mode": "content|files_with_matches|count",
            "glob": "str",
            "type": "str",
            "-A": "int",
            "-B": "int",
            "-C": "int",
            "context": "int",
            "n": "bool",
            "i": "bool",
            "case_insensitive": "bool",
            "multiline": "bool",
            "head_limit": "int",
            "offset": "int",
        },
    },
    {
        "name": "Write",
        "description": "写入文件（覆盖）",
        "parameters": {"file_path": "str", "content": "str"},
    },
    {
        "name": "Edit",
        "description": "按精确字符串替换修改文件",
        "parameters": {
            "file_path": "str",
            "old_string": "str",
            "new_string": "str",
            "replace_all": "bool",
        },
    },
]


class ConfigError(RuntimeError):
    """判分环境不完整（缺模型凭据 / 未装 py-openjudge）。"""


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    key: str
    grader: str
    weight: float
    low: float
    high: float
    rubric: str = ""


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    judges: dict[str, JudgeSpec] = field(default_factory=dict)

    @property
    def weights(self) -> dict[str, float]:
        return {key: spec.weight for key, spec in self.judges.items()}


def load_plan(path: Path | None = None) -> Plan:
    """读 plan.yaml；权重和必须为 1（否则「归一化总分」不可比）。"""
    target = path or PLAN_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    judges: dict[str, JudgeSpec] = {}
    for key, spec in (raw.get("judges") or {}).items():
        scale = spec.get("scale") or [0, 1]
        judges[str(key)] = JudgeSpec(
            key=str(key),
            grader=str(spec["grader"]),
            weight=float(spec.get("weight", 0)),
            low=float(scale[0]),
            high=float(scale[1]),
            rubric=str(spec.get("rubric", "")),
        )
    total = sum(spec.weight for spec in judges.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"plan 权重和必须为 1，当前 {total}")
    return Plan(name=str(raw.get("plan", "unnamed")), judges=judges)


def plan_sha(plan: Plan) -> str:
    import hashlib

    blob = json.dumps(
        {key: (s.grader, s.weight, s.low, s.high, s.rubric) for key, s in plan.judges.items()},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _build_grader(spec: JudgeSpec, model: Any) -> Any:
    """按 plan 里的类名构造 Grader；未知类名与未冻结 rubric 都在加载期报错。"""
    from openjudge.graders.agent.action.action_loop import ActionLoopDetectionGrader
    from openjudge.graders.agent.tool.tool_parameter_check import ToolParameterCheckGrader
    from openjudge.graders.agent.tool.tool_selection import ToolSelectionGrader
    from openjudge.graders.common.correctness import CorrectnessGrader
    from openjudge.graders.common.instruction_following import InstructionFollowingGrader
    from openjudge.graders.common.relevance import RelevanceGrader
    from openjudge.models.schema.prompt_template import LanguageEnum

    items = _frozen_rubric_items(spec.rubric) if spec.rubric else []
    if items and spec.grader == "ActionLoopDetectionGrader":
        raise ConfigError("ActionLoopDetectionGrader 不支持 rubric 注入")
    template = None
    if items:
        defaults = {
            "CorrectnessGrader": CorrectnessGrader,
            "RelevanceGrader": RelevanceGrader,
            "InstructionFollowingGrader": InstructionFollowingGrader,
            "ToolSelectionGrader": ToolSelectionGrader,
            "ToolParameterCheckGrader": ToolParameterCheckGrader,
        }
        source = defaults.get(spec.grader)
        if source is None:
            raise ValueError(f"plan 中未知 Grader: {spec.grader}")
        template = _rubric_template(source.DEFAULT_TEMPLATE, items)

    if spec.grader == "ActionLoopDetectionGrader":
        return ActionLoopDetectionGrader()
    table = {
        "CorrectnessGrader": lambda: CorrectnessGrader(
            model=model, language=LanguageEnum.ZH, template=template
        ),
        "RelevanceGrader": lambda: RelevanceGrader(
            model=model, language=LanguageEnum.ZH, template=template
        ),
        "InstructionFollowingGrader": lambda: InstructionFollowingGrader(
            model=model, language=LanguageEnum.ZH, template=template
        ),
        "ToolSelectionGrader": lambda: ToolSelectionGrader(
            model=model, language=LanguageEnum.ZH, template=template
        ),
        "ToolParameterCheckGrader": lambda: ToolParameterCheckGrader(
            model=model, language=LanguageEnum.ZH, template=template
        ),
    }
    builder = table.get(spec.grader)
    if builder is None:
        raise ValueError(f"plan 中未知 Grader: {spec.grader}")
    return builder()


def _messages(trace: TraceRecord) -> list[dict[str, Any]]:
    """OpenAI 风格的 action-observation 消息，供 loop 检测与工具裁判。"""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": trace.turns[0]["user"] if trace.turns else ""}
    ]
    for call in trace.tool_calls:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"{call.name}-{len(messages)}",
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.args, ensure_ascii=False),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"{call.name}-{len(messages) - 1}",
                "content": call.result_excerpt,
            }
        )
    messages.append({"role": "assistant", "content": trace.final_text})
    return messages


def _last_user(case: Case) -> str:
    """最后一条真正的用户指令（跳过确认轮）。

    多轮用例里第一句只是上下文，裁判要评的是「最后这问答得怎么样」；
    继续拿 case.prompt 会把「记住上面的清单」当成指令去评「第 3 条是什么」，
    instruction 分必然偏低。
    """
    for turn in reversed(case.turns):
        if not turn.is_confirm:
            return turn.text
    return case.prompt


def _row(case: Case, trace: TraceRecord) -> dict[str, Any]:
    query = _last_user(case)
    return {
        "query": query,
        "instruction": query,
        "response": trace.final_text,
        "reference_response": str(case.judge.get("reference_response", "")),
        "tool_definitions": TOOL_DEFINITIONS,
        "tool_calls": [{"name": call.name, "arguments": call.args} for call in trace.tool_calls],
        "messages": _messages(trace),
    }


def _score_of(result: Any) -> float | None:
    score = getattr(result, "score", None)
    if isinstance(score, (int, float)):
        return float(score)
    return None


def _reason_of(result: Any) -> str:
    return str(getattr(result, "reason", "") or getattr(result, "error", "") or "")


def _normalize(score: float, spec: JudgeSpec) -> float:
    span = spec.high - spec.low
    if span <= 0:
        return 0.0
    return min(1.0, max(0.0, (score - spec.low) / span))


async def _run_group(
    plan: Plan,
    keys: tuple[str, ...],
    model: Any,
    rows: list[dict[str, Any]],
) -> list[dict[str, dict[str, Any]]]:
    """对一组用例跑指定 Grader，返回与 rows 对齐的结果数组。"""
    from openjudge.runner.grading_runner import GraderConfig, GradingRunner

    if not rows:
        return []
    configs = {key: GraderConfig(grader=_build_grader(plan.judges[key], model)) for key in keys}
    runner = GradingRunner(grader_configs=configs, max_concurrency=6, show_progress=False)
    result = await runner.arun(rows)
    graded: list[dict[str, dict[str, Any]]] = [{} for _ in rows]
    for key, items in result.items():
        spec = plan.judges[key]
        for index, item in enumerate(items[: len(rows)]):
            score = _score_of(item)
            graded[index][key] = {
                "status": "scored",
                "score": score,
                "normalized": _normalize(score, spec) if score is not None else None,
                "reason": _reason_of(item),
                "weight": spec.weight,
            }
    return graded


async def grade_traces(
    cases: list[Case],
    traces: list[TraceRecord],
    *,
    plan: Plan | None = None,
    model: Any | None = None,
    judge: bool = True,
    judge_model_name: str = "",
    judge_gate: bool = True,
) -> list[dict[str, Any]]:
    """对轨迹逐条判分；`judge=False` 只跑规则门禁（离线 PR 用）。"""
    active_plan = plan or load_plan()
    by_id = {case.id: case for case in cases}
    results: list[dict[str, Any]] = []
    for trace in traces:
        case = by_id.get(trace.case_id)
        if case is None:
            continue
        # step1: 每条先跑确定性断言
        checks = run_checks(case, trace)
        results.append(
            {
                "case_id": trace.case_id,
                "track": case.track,
                "risk": case.risk,
                "subject": case.subject,
                "status": trace.status,
                "rule_passed": rule_passed(checks),
                "violations": violations(checks),
                "checks": [check.to_dict() for check in checks],
                "judge": {},
                "judge_norm": None,
            }
        )

    # step2: judge=False 到此为止
    if not judge:
        _finish(results, judge_gate=judge_gate)
        return results

    # step3: 需裁判则构造模型，缺配置抛 ConfigError
    if model is None:
        model = judge_model(judge_model_name)
    # step4: 语义分并回后算 passed
    await _apply_judge(active_plan, model, cases, traces, results)
    _finish(results, judge_gate=judge_gate)
    return results


def _finish(results: list[dict[str, Any]], *, judge_gate: bool = True) -> None:
    """合并规则与语义结论，得出单条用例是否通过。

    单条门槛 0.60 而不是 0.75：裁判与被测模型同源（deepseek-v4.1-flash），
    1–5 分制下「正确但略有增补」普遍落在 3–4 分，归一化后 0.5–0.75；
    把门槛钉在 0.75 等于要求近乎满分，会把正确回答判失败。
    赛道级 judge 均值门槛仍单独由 report.TRACK_RULES 控制。
    """
    for result in results:
        judged = result["judge"]
        result["judge_norm"] = total_score(judged) if judged else None
        judge_ok = (
            not judge_gate
            or result["judge_norm"] is None
            or result["judge_norm"] >= PASS_JUDGE
        )
        result["passed"] = bool(result["rule_passed"]) and judge_ok


_JUDGE_GROUPS = ("content", "instruction", "tool", "loop")


def _judge_row(
    case: Case, result: dict[str, Any] | None, trace: TraceRecord | None
) -> dict[str, Any] | None:
    """只有规则通过、且用例声明了 judge 的才返回待判行，其余跳过。"""
    if result is None or trace is None or not result["rule_passed"] or not case.judge:
        return None
    return _row(case, trace)


def _add_row(
    bucket: tuple[list[dict[str, Any]], list[str]], case_id: str, row: dict[str, Any]
) -> None:
    bucket[0].append(row)
    bucket[1].append(case_id)


def _fill_buckets(
    case: Case,
    trace: TraceRecord,
    row: dict[str, Any],
    buckets: dict[str, tuple[list[dict[str, Any]], list[str]]],
) -> None:
    """按适用性把用例投进不同裁判组（见 _apply_judge 的分组裁剪说明）。"""
    if row["reference_response"]:
        _add_row(buckets["content"], case.id, row)
        if not any(turn.is_confirm for turn in case.turns):
            _add_row(buckets["instruction"], case.id, row)
    wanted = set(case.expected.tool_args)
    calls = [call for call in trace.tool_calls if call.name in wanted]
    if calls and case.judge.get("tool_check", True):
        tool_row = dict(row)
        tool_row["tool_calls"] = [{"name": call.name, "arguments": call.args} for call in calls]
        _add_row(buckets["tool"], case.id, tool_row)
    if len(trace.tool_calls) >= 2:
        _add_row(buckets["loop"], case.id, row)


async def _apply_judge(
    plan: Plan,
    model: Any,
    cases: list[Case],
    traces: list[TraceRecord],
    results: list[dict[str, Any]],
) -> None:
    """只对规则通过且声明了 judge 的用例跑语义裁判。

    分组裁剪（避免把「不适用」判成 0 分）：
    - 工具组只在用例显式声明了期望工具/入参时跑，且只把命中的调用交给裁判；
    - 确认类用例不跑 instruction：确认/拒绝是用户决定，用第一句操作指令
      去评「有没有照做」必然冤枉模型；
    - loop 需要至少两次工具调用才有意义。
    """
    # step1: 过滤规则失败/无 judge 者
    trace_by_id = {trace.case_id: trace for trace in traces}
    result_by_id = {result["case_id"]: result for result in results}
    buckets: dict[str, tuple[list[dict[str, Any]], list[str]]] = {
        name: ([], []) for name in _JUDGE_GROUPS
    }
    # step2: 先写显式 skipped，再按适用性投四桶
    write_skip_entries(plan, cases, trace_by_id, result_by_id, results)
    for case in cases:
        trace = trace_by_id.get(case.id)
        row = _judge_row(case, result_by_id.get(case.id), trace)
        if row is None or trace is None:
            continue
        _fill_buckets(case, trace, row, buckets)

    # step3: 四桶各一个 GradingRunner 并发
    groups = await asyncio.gather(
        _run_group(plan, ("correctness", "relevance"), model, buckets["content"][0]),
        _run_group(plan, ("instruction",), model, buckets["instruction"][0]),
        _run_group(plan, ("tool_select", "tool_param"), model, buckets["tool"][0]),
        _run_group(plan, ("loop",), model, buckets["loop"][0]),
    )
    # step4: 多组结果 update 合并不覆盖
    for name, graded in zip(_JUDGE_GROUPS, groups, strict=False):
        for case_id, item in zip(buckets[name][1], graded, strict=False):
            # 同一用例可能同时被多组判分，合并而不是覆盖
            result_by_id[case_id]["judge"].update(item)
    # step5: 仍未出现的键补 skipped，判分方案里的六项始终可见
    fill_remaining_skips(plan, results)
