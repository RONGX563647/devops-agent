"""数据库行到数据对象的转换。"""

from __future__ import annotations

import sqlite3

from devops_agent.observability.models import (
    EvalScoreRecord,
    InputStageRecord,
    RunRecord,
    SpanRecord,
)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        domain=row["domain"],
        status=row["status"],
        input=row["input"],
        output=row["output"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_ms=row["duration_ms"],
        model=row["model"],
        total_tokens=row["total_tokens"],
        health_score=row["health_score"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_input_tokens=row["cache_input_tokens"],
        cache_creation_input_tokens=row["cache_creation_input_tokens"],
        request_id=row["request_id"],
        permission=row["permission"] or "",
        work_ms=row["work_ms"] or 0,
        wall_ms=row["wall_ms"] or 0,
        error=row["error"] or "",
        # 老记录补列后是空串：读出来表示「未记录」，不假装是默认参数
        model_params=row["model_params"] or "",
        web_access=row["web_access"] or "",
        workspace_id=row["workspace_id"] or "",
    )


def _span_from_row(row: sqlite3.Row) -> SpanRecord:
    return SpanRecord(
        id=row["id"],
        run_id=row["run_id"],
        type=row["type"],
        name=row["name"],
        start_offset_ms=row["start_offset_ms"],
        duration_ms=row["duration_ms"],
        input=row["input"],
        output=row["output"],
        tokens=row["tokens"],
        status=row["status"],
        error=row["error"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_input_tokens=row["cache_input_tokens"],
        cache_creation_input_tokens=row["cache_creation_input_tokens"],
        seq=row["seq"] or 0,
        parent_id=row["parent_id"],
        ttft_ms=row["ttft_ms"] or 0,
    )


def _input_stage_from_row(row: sqlite3.Row) -> InputStageRecord:
    return InputStageRecord(
        id=row["id"],
        run_id=row["run_id"],
        seq=row["seq"],
        stage=row["stage"],
        label=row["label"],
        status=row["status"],
        changed=bool(row["changed"]),
        summary=row["summary"],
        before=row["before_chars"],
        after=row["after_chars"],
        items=row["items"],
        duration_ms=row["duration_ms"],
        detail=row["detail"],
        failed=row["failed"],
    )


def _score_from_row(row: sqlite3.Row) -> EvalScoreRecord:
    return EvalScoreRecord(
        id=row["id"],
        run_id=row["run_id"],
        dimension=row["dimension"],
        score=row["score"],
        source=row["source"],
        rater=row["rater"],
        created_at=row["created_at"],
        comment=row["comment"],
    )


def _eval_batch_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "suite": str(row["suite"]),
        "suite_version": str(row["suite_version"] or ""),
        "suite_sha": str(row["suite_sha"] or ""),
        "prompt_id": str(row["prompt_id"] or ""),
        "prompt_sha": str(row["prompt_sha"] or ""),
        "model": str(row["model"] or ""),
        "profile": str(row["profile"] or ""),
        "started_at": str(row["started_at"]),
        "finished_at": row["finished_at"],
        "status": str(row["status"]),
        "summary_json": str(row["summary_json"] or ""),
    }


def _eval_case_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "batch_id": int(row["batch_id"]),
        "case_id": str(row["case_id"]),
        "run_id": row["run_id"],
        "status": str(row["status"]),
        "passed": bool(row["passed"]),
        "scores_json": str(row["scores_json"] or ""),
        "detail_json": str(row["detail_json"] or ""),
        "latency_ms": int(row["latency_ms"]),
        "tokens": int(row["tokens"]),
    }
