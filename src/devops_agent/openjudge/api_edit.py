"""OpenJudge 的可写接口：评测配置（模型）与用例增删改。

与 `api.py` 分开的理由：那边全是「读批次、起任务」，这边是「改配置、改题库」。
两类操作的失败处理完全不同 —— 读接口坏就返回空，写接口必须 422 且**什么都不落盘**。

三条约定：

- 写用例只碰 `workspace/openjudge/cases/`（覆盖层），永远不改 git 里的题库文件；
- 结构非法的用例在解析阶段就被拒，不会出现「跑起来才发现少字段」；
- 配置改动只落 `oj_settings`，不动 `settings['model_config']`（日常对话的模型）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException
from fastapi import Path as PathParam

from devops_agent.openjudge import benchmark, casestore, report, service, settings

router = APIRouter(prefix="/api/openjudge", tags=["openjudge-edit"])

CaseId = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9._-]{1,64}$")]


def _limits() -> dict[str, Any]:
    """前端画控件用的边界值；不在这里再写死一份。"""
    return {
        "max_concurrency": service.MAX_CONCURRENCY,
        "min_timeout": settings.MIN_TIMEOUT,
        "max_timeout": settings.MAX_TIMEOUT,
        "max_limit": service.MAX_LIMIT,
        "tracks": list(benchmark.TRACKS),
        "risks": list(benchmark.RISKS),
        "permissions": list(benchmark.PERMISSIONS),
    }


def _settings_payload(values: dict[str, Any]) -> dict[str, Any]:
    return {"values": values, "effective": settings.resolved(), "limits": _limits()}


@router.get("/settings")
def read_settings() -> dict[str, Any]:
    """当前评测配置 + 实际生效的模型名。

    `effective` 单独给：`values` 里空字符串表示「不覆盖」，用户需要看到它最后
    落到了哪个模型上，否则「我明明没填为什么在跑」无法解释。
    """
    return _settings_payload(settings.load())


@router.patch("/settings")
def patch_settings(patch: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
    """局部更新配置（未出现的字段保持原值）。"""
    try:
        values = settings.save(patch)
    except settings.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _settings_payload(values)


@router.get("/cases")
def list_cases(track: str = "", source: str = "") -> dict[str, Any]:
    """题库列表（含本地覆盖层）；`source` 可过滤 repo / overlay。"""
    rows = casestore.list_cases()
    if track:
        rows = [row for row in rows if row["track"] == track.strip().upper()]
    if source:
        rows = [row for row in rows if row["source"] == source]
    return {
        "items": rows,
        "total": len(rows),
        "suite": casestore.suite_view(),
        "thresholds": report.TRACK_RULES,
        "limits": _limits(),
    }


@router.get("/cases/{case_id}")
def read_case(case_id: CaseId) -> dict[str, Any]:
    raw = casestore.get_raw(case_id)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"用例不存在：{case_id}")
    return {"case": raw, "limits": _limits()}


@router.put("/cases/{case_id}")
def write_case(case_id: CaseId, raw: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
    """新增或修改一条用例；改 id 会被拒绝（改名等于删一条加一条，别偷偷做）。"""
    if str(raw.get("id", case_id)) != case_id:
        raise HTTPException(status_code=422, detail="请求体里的 id 与路径不一致，不支持改名改题")
    payload = {**raw, "id": case_id}
    try:
        casestore.save(payload)
    except casestore.CaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"case": casestore.get_raw(case_id), "suite": casestore.suite_view()}


@router.delete("/cases/{case_id}")
def delete_case(case_id: CaseId) -> dict[str, Any]:
    """从题库隐藏一条（仓库里的用例只是记进隐藏清单，不删文件）。"""
    if not casestore.delete(case_id):
        raise HTTPException(status_code=404, detail=f"用例不存在：{case_id}")
    return {"id": case_id, "deleted": True, "suite": casestore.suite_view()}


@router.post("/cases/{case_id}/restore")
def restore_case(case_id: CaseId) -> dict[str, Any]:
    """丢弃本地改动，回到仓库版本。"""
    if not casestore.restore(case_id):
        raise HTTPException(status_code=404, detail=f"用例不存在：{case_id}")
    return {"id": case_id, "case": casestore.get_raw(case_id), "suite": casestore.suite_view()}
