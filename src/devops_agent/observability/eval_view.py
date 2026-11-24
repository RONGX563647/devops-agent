"""评测只读 API 的「行 → JSON」映射层。

拆自 `eval_api.py`：路由负责取数与错误兜底，字段摊平与口径归一放在这里，
两边都能各自守住行数门禁，字段改动也不必再动路由。
"""

from __future__ import annotations

import json
from typing import Any

from devops_agent.eval.kinds import BLOCKING_KEYS, HARD_KEYS, labels, zero_counts


def _as_dict(value: object) -> dict[str, Any]:
    """把 JSON 字符串解析成 dict；脏数据一律回退空 dict。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _to_int(value: object) -> int:
    """把 DB 里的 object 安全转成 int。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _text(row: dict[str, object], key: str) -> str:
    return str(row.get(key) or "")


def _summary_int(summary: dict[str, object], key: str) -> int:
    return _to_int(summary.get(key))


def _summary_float(summary: dict[str, object], key: str) -> float:
    """把 summary 里的 object 安全转成 float；脏数据回退 0.0。"""
    value = summary.get(key)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _hard_failures(summary: dict[str, object]) -> dict[str, int]:
    """7 类硬失败计数；旧批次缺的键补 0，前端不必处理缺字段。"""
    hard = summary.get("hard_failures")
    if not isinstance(hard, dict):
        return zero_counts()
    return {key: int(hard.get(key, 0) or 0) for key in HARD_KEYS}


def _hard_split(summary: dict[str, object]) -> tuple[int, int]:
    """(阻断类失败数, 计分类失败数)。

    优先用 summary 里已有的汇总值；旧批次没有这两个字段时按分类计数重算，
    避免前端拿到 0 就显示「硬门槛通过」。
    """
    blocking = _summary_int(summary, "hard_blocking")
    scoring = _summary_int(summary, "hard_scoring")
    if "hard_blocking" in summary or "hard_scoring" in summary:
        return blocking, scoring
    hard = _hard_failures(summary)
    return (
        sum(count for key, count in hard.items() if key in BLOCKING_KEYS),
        sum(count for key, count in hard.items() if key not in BLOCKING_KEYS),
    )


def _verdict(row: dict[str, object], summary: dict[str, object]) -> str:
    verdict = str(summary.get("verdict") or "")
    # 运行中的批次还没 summary，前端要能显示 running 而不是空徽标
    if not verdict and row.get("status") == "running":
        return "running"
    return verdict


def _batch_out(row: dict[str, object]) -> dict[str, Any]:
    """把 DB 行 + summary_json 摊平成稳定响应字段。"""
    summary = _as_dict(row.get("summary_json"))
    judge = summary.get("judge")
    gates = summary.get("gates")
    compatibility = summary.get("compatibility")
    hard_blocking, hard_scoring = _hard_split(summary)
    return {
        "id": _to_int(row["id"]),
        "suite": _text(row, "suite"),
        "suite_version": _text(row, "suite_version"),
        "suite_sha": _text(row, "suite_sha"),
        "prompt_id": _text(row, "prompt_id"),
        "prompt_hash": _text(row, "prompt_sha"),
        "model": _text(row, "model"),
        "profile": _text(row, "profile"),
        "started_at": _text(row, "started_at"),
        "finished_at": row.get("finished_at"),
        "status": _text(row, "status"),
        "n": _summary_int(summary, "n"),
        "passed": _summary_int(summary, "passed"),
        "pass_rate": _summary_float(summary, "pass_rate"),
        # v3 能力口径：安全 ∧ 结果 ∧ 工具路线，引用/预算不否决；
        # attributed 只统计模型/安全归因样本，harness/assertion 不背锅。
        "capability_passed": _summary_int(summary, "capability_passed"),
        "capability_pass_rate": _summary_float(summary, "capability_pass_rate"),
        **{key: summary.get(key) for key in ("attributed_pass_rate", "tokens_per_solved_case")},
        "risk": summary.get("risk") if isinstance(summary.get("risk"), dict) else {},
        "funnel": summary.get("funnel") if isinstance(summary.get("funnel"), list) else [],
        "attribution": (
            summary.get("attribution") if isinstance(summary.get("attribution"), dict) else {}
        ),
        "hard_failures": _hard_failures(summary),
        # 阻断类才拦合并；计分类只进 pass_rate
        "hard_blocking": hard_blocking,
        "hard_scoring": hard_scoring,
        "hard_labels": labels(),
        "judge": judge if isinstance(judge, dict) else None,
        "trajectory_score": summary.get("trajectory_score"),
        "p95_ms": _summary_int(summary, "p95_ms"),
        "tokens_total": _summary_int(summary, "tokens_total"),
        # 前缀缓存：命中率是「提示词布局改动是否毁了缓存」的唯一可观察口径
        "cache_input": _summary_int(summary, "cache_input"),
        "cache_creation": _summary_int(summary, "cache_creation"),
        "cache_hit_rate": summary.get("cache_hit_rate"),
        "cost_total": _summary_float(summary, "cost_total"),
        "baseline_id": summary.get("baseline_id"),
        "verdict": _verdict(row, summary),
        # 门禁三态与可比性：前端据此解释「为什么是这个结论」，
        # 不在前端重算阈值 —— 两套判定逻辑会互相打脸。
        "gates": gates if isinstance(gates, list) else [],
        "min_cases_for_gate": _summary_int(summary, "min_cases_for_gate"),
        "mde": summary.get("mde"),
        "compatibility": compatibility if isinstance(compatibility, dict) else None,
        "grader_version": str(summary.get("grader_version") or ""),
    }


def _case_out(row: dict[str, object]) -> dict[str, Any]:
    """把用例行 + detail_json / scores_json 摊平。"""
    detail = _as_dict(row.get("detail_json"))
    scores = _as_dict(row.get("scores_json"))
    judge = scores or detail.get("judge")
    tags = detail.get("tags")
    hard = detail.get("hard_failures")
    return {
        "case_id": str(row.get("case_id") or ""),
        "status": str(row.get("status") or ""),
        "passed": bool(row.get("passed")),
        # 单例四元组：答对、安全、路线、引用分开看，前端不再只有一个 passed。
        **{
            key: detail.get(key)
            for key in (
                "risk",
                "safety_ok",
                "answer_ok",
                "trajectory_ok",
                "format_ok",
                "attribution",
                "attribution_detail",
            )
        },
        "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
        "trajectory_score": detail.get("trajectory_score"),
        "judge": judge if isinstance(judge, dict) else {},
        "latency_ms": _to_int(row.get("latency_ms")),
        "tokens": _to_int(row.get("tokens")),
        "checks": _as_list(detail.get("checks")),
        "run_id": row.get("run_id"),
        # 三档工具轨迹：叫过 / 跑过 / 被拒。前端据此区分
        # 「模型没试」与「试了但权限拒绝」——这两者在旧契约里长得一样。
        "tool_names": _as_list(detail.get("tool_names")),
        "executed_tools": _as_list(detail.get("executed_tools")),
        "denied_tools": _as_list(detail.get("denied_tools")),
        "hard_failures": [str(kind) for kind in hard] if isinstance(hard, list) else [],
        "error": detail.get("error"),
    }
