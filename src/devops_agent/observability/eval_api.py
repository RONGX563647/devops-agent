"""评测只读 API —— 批次列表、批次详情、用例分页与基线对比。

设计：
- 只读：不提供写入口，评测写入由 CLI 负责；
- 数据库不可用（文件不可写、表缺失、路径异常）时返回空列表或 404，
  不把运维问题放大成 500；
- 字段名与 CLI 产出的 summary_json 对齐，前端不需要自己解 JSON。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Query

from devops_agent.eval import report as report_mod
from devops_agent.eval.dataset import EvalCase, Suite, ToolExpectations, load_suite
from devops_agent.observability import repository as repo
from devops_agent.observability.eval_view import (
    _as_dict,
    _batch_out,
    _case_out,
    _hard_failures,
    _hard_split,
    _summary_float,
    _summary_int,
    _text,
    _to_int,
    _verdict,
)

router = APIRouter(prefix="/api/eval", tags=["eval"])

T = TypeVar("T")

# 题库目录：eval_api.py -> observability -> devops_agent -> src -> 仓库根
_SUITES_DIR = Path(__file__).resolve().parents[3] / "eval" / "suites"


def _read(operation: Callable[[sqlite3.Connection], T]) -> T | None:
    """在短连接里执行只读操作；数据库不可用返回 None。"""
    try:
        with repo.connect(repo.eval_db_path()) as conn:
            repo.init_db(conn)
            return operation(conn)
    except (sqlite3.Error, OSError):
        return None


def _extend_assertions(items: list[dict[str, str]], kind: str, targets: Sequence[str]) -> None:
    items.extend({"kind": kind, "target": target} for target in targets)


def _tool_assertions(calls: ToolExpectations | None) -> list[dict[str, str]]:
    """工具类断言：必用 / 禁用 / 调用预算。"""
    if calls is None:
        return []
    items: list[dict[str, str]] = []
    _extend_assertions(items, "tool_must", calls.must)
    _extend_assertions(items, "tool_forbid", calls.forbid)
    if calls.max_total is not None:
        items.append({"kind": "tool_budget", "target": str(calls.max_total)})
    return items


def _case_assertions(case: EvalCase) -> list[dict[str, str]]:
    """一条用例的断言清单（类型 + 目标），前端展开用例时逐条显示。"""
    expected = case.expected
    items: list[dict[str, str]] = []
    _extend_assertions(items, "must_contain", expected.must_contain)
    _extend_assertions(
        items, "must_contain_any", [" / ".join(group) for group in expected.must_contain_any]
    )
    _extend_assertions(items, "must_not_contain", expected.must_not_contain)
    _extend_assertions(items, "must_not_regex", expected.must_not_regex)
    _extend_assertions(items, "must_cite", expected.must_cite)
    _extend_assertions(items, "regex", expected.regex)
    _extend_assertions(items, "file_state", [item.path for item in expected.file_state])
    items.extend(_tool_assertions(expected.tool_calls))
    return items


def _case_decision(case: EvalCase) -> dict[str, Any]:
    """该用例的档位可达性：`command_hint` 在声明的档位下判定为什么。

    前端在评测集面板里显示这个徽标，回答「这条用例为什么不可能通过」。
    延迟 import 的原因同 `_suite_out`：lint 会拖入框架。

    三态而不是布尔，与 lint S8 的分级对齐：
    - `ok`：档位判定与期望一致；
    - `mismatch`：档位与期望不符（lint 里是 error，用例结构上不可能通过）；
    - `defense_in_grader`：`require_executed=False` 且权限层放行——
      用例本身能跑，但它的防线不在权限层（lint 里是 warning）。
    """
    from devops_agent.eval.lint import _achievable  # noqa: PLC0415 - 见上

    problem = _achievable(case)
    if problem is None:
        status = "ok"
    elif case.require_executed:
        status = "mismatch"
    else:
        status = "defense_in_grader"
    return {"command_hint": case.command_hint, "decision": status, "problem": problem}


def _suite_out(suite: Suite) -> dict[str, Any]:
    """评测集目录：构成、阈值、断言清单，供前端画配比与断言构成。"""
    # 延迟 import：lint 会拖入 runtime.prompt（进而 import agentscope），
    # 而 observability 层不该在启动时就绑定框架。
    from devops_agent.eval.lint import assertion_summary

    manifest = suite.manifest
    subjects: dict[str, int] = {}
    for case in suite.cases:
        subjects[case.subject] = subjects.get(case.subject, 0) + 1
    judged = sum(1 for case in suite.cases if case.judge is not None)
    return {
        "suite": manifest.suite,
        "version": manifest.version,
        "sha256": manifest.sha256,
        "counts": manifest.counts,
        "min_cases_for_gate": manifest.min_cases_for_gate,
        "grader_version": manifest.grader_version,
        "thresholds": {
            key: {"min": spec.min, "min_samples": spec.min_samples}
            for key, spec in manifest.thresholds.items()
        },
        "assertions": assertion_summary(suite),
        # 按 subject 归因与裁判覆盖：前端据此回答
        # 「这次变化能归到哪个因子」「裁判样本够不够」
        "subjects": subjects,
        "judged_cases": judged,
        "cases": [
            {
                "id": case.id,
                "tags": list(case.tags),
                "permission": case.permission,
                "subject": case.subject,
                "require_executed": case.require_executed,
                "assertions": _case_assertions(case),
                **_case_decision(case),
            }
            for case in suite.cases
        ],
    }


@router.get("/suites")
def list_suites() -> dict[str, Any]:
    """本仓的评测集目录（只读文件，不碰 DB）。

    读不到或解析失败的那个 suite 直接跳过：一个坏文件不该让整页打不开。
    """
    items: list[dict[str, Any]] = []
    for path in sorted(_SUITES_DIR.glob("*.jsonl")):
        try:
            items.append(_suite_out(load_suite(path)))
        except (OSError, ValueError):
            continue
    return {"items": items}


def _series_point(row: dict[str, object]) -> dict[str, Any]:
    """批次时间序列的一个点：趋势图只需要这几个数。"""
    summary = _as_dict(row.get("summary_json"))
    judge = summary.get("judge")
    correctness = judge.get("correctness") if isinstance(judge, dict) else None
    hard = _hard_failures(summary)
    blocking, scoring = _hard_split(summary)
    return {
        "id": _to_int(row["id"]),
        "started_at": _text(row, "started_at"),
        "n": _summary_int(summary, "n"),
        "pass_rate": _summary_float(summary, "pass_rate"),
        "judge_correctness": correctness,
        "trajectory_score": summary.get("trajectory_score"),
        "p95_ms": _summary_int(summary, "p95_ms"),
        "hard_total": sum(hard.values()),
        # 阻断/计分分开：趋势图上「阻断归零但计分上升」是完全不同的故事
        "hard_blocking": blocking,
        "hard_scoring": scoring,
        "verdict": _verdict(row, summary),
        "prompt_hash": _text(row, "prompt_sha"),
        # 趋势图要看「缓存命中率有没有掉」：提示词布局改坏时它先掉
        "cache_hit_rate": summary.get("cache_hit_rate"),
    }


@router.get("/series")
def list_series(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """最近 N 个批次的时间序列（按时间升序，方便直接画折线）。"""
    batches = _read(lambda conn: repo.list_eval_batches(conn, limit=limit))
    if not batches:
        return {"items": []}
    # 仓储按时间倒序返回，趋势图要正序
    return {"items": [_series_point(row) for row in reversed(batches)]}


@router.get("/matrix")
def case_matrix(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    """用例 × 批次通过矩阵：回答「整体退化还是某条一直挂」。

    cells 三态：`true` 通过 / `false` 失败 / `null` 该批次没跑到这条。
    """

    def load(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        columns: list[dict[str, Any]] = []
        for row in reversed(repo.list_eval_batches(conn, limit=limit)):
            batch_id = _to_int(row["id"])
            cases = repo.list_eval_cases(conn, batch_id, limit=1000)
            columns.append(
                {
                    "id": batch_id,
                    "started_at": _text(row, "started_at"),
                    "cases": {
                        str(case.get("case_id") or ""): bool(case.get("passed")) for case in cases
                    },
                }
            )
        return columns

    columns = _read(load) or []
    case_ids = sorted({case_id for column in columns for case_id in column["cases"]})
    return {
        "batches": [{"id": column["id"], "started_at": column["started_at"]} for column in columns],
        "cases": [
            {
                "case_id": case_id,
                "cells": [column["cases"].get(case_id) for column in columns],
            }
            for case_id in case_ids
        ],
    }


@router.get("/batches")
def list_batches(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """最近评测批次。"""

    def load(conn: sqlite3.Connection) -> tuple[list[dict[str, object]], int]:
        return repo.list_eval_batches(conn, limit=limit, offset=offset), repo.count_eval_batches(
            conn
        )

    result = _read(load)
    if result is None:
        return {"items": [], "total": 0}
    rows, total = result
    return {"items": [_batch_out(row) for row in rows], "total": total}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    """批次详情 + 标签分布 + 归因分布 + 前 50 条用例。"""
    batch = _read(lambda conn: repo.get_eval_batch(conn, batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")
    cases = _read(lambda conn: repo.list_eval_cases(conn, batch_id, limit=50)) or []
    summary = _as_dict(batch.get("summary_json"))
    tags = summary.get("tags")
    subjects = summary.get("subjects")
    return {
        "batch": _batch_out(batch),
        "tags": tags if isinstance(tags, dict) else {},
        # 按 subject 归因：回答「这次分数变化能算到哪个被测因子」
        "subjects": subjects if isinstance(subjects, dict) else {},
        "cases": [_case_out(row) for row in cases],
    }


@router.get("/batches/{batch_id}/cases")
def list_cases(
    batch_id: int,
    status: str | None = None,
    tag: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """按状态 / 标签分页列用例。"""
    batch = _read(lambda conn: repo.get_eval_batch(conn, batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="batch_not_found")

    def load(conn: sqlite3.Connection) -> tuple[list[dict[str, object]], int]:
        return repo.list_eval_cases(
            conn, batch_id, status=status, tag=tag, limit=limit, offset=offset
        ), repo.count_eval_cases(conn, batch_id, status=status, tag=tag)

    result = _read(load) or ([], 0)
    rows, total = result
    return {"items": [_case_out(row) for row in rows], "total": total}


def _summary_for_compare(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    """取 summary_json 并补上用例通过情况，供 compare 计算 case diff。"""
    row = repo.get_eval_batch(conn, batch_id)
    if row is None:
        return None
    summary = _as_dict(row.get("summary_json"))
    cases = repo.list_eval_cases(conn, batch_id, limit=10000)
    summary["cases"] = [
        {"case_id": str(case.get("case_id") or ""), "passed": bool(case.get("passed"))}
        for case in cases
    ]
    return summary


@router.get("/compare")
def compare_batches(
    base: int = Query(..., ge=1),
    head: int = Query(..., ge=1),
) -> dict[str, Any]:
    """对比两个批次的指标与用例变化。"""

    def load(conn: sqlite3.Connection) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return _summary_for_compare(conn, base), _summary_for_compare(conn, head)

    result = _read(load)
    if result is None:
        return {"verdict": "insufficient", "metrics": [], "cases": []}
    base_summary, head_summary = result
    if base_summary is None:
        raise HTTPException(status_code=404, detail="base_batch_not_found")
    if head_summary is None:
        raise HTTPException(status_code=404, detail="head_batch_not_found")
    compared = report_mod.compare(head_summary, base_summary)
    return {
        "verdict": compared["verdict"],
        "metrics": compared["metrics"],
        "cases": compared["cases"],
    }
