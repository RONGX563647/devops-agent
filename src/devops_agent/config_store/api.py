"""配置中心端点 —— 一个域一份快照，写入走增量 + 乐观锁。

约定与 `model_api` 保持一致，避免同一个项目里出现两套语义：

1. 服务端是唯一裁判：越界 400，附 `{code, message}`，界面据此高亮。
2. `null` 表示删掉覆盖、回落到下层，不是「值等于 null」。
3. 不谎报生效范围：`hot` 直接来自注册表（immediate / next_turn / restart）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from devops_agent import config_store
from devops_agent.config_store import store
from devops_agent.config_store.registry import BY_KEY, DomainSpec, env_overlay

router = APIRouter()


class PatchRequest(BaseModel):
    """增量修改；`version` 省略表示不校验并发（脚本 / 首次写入）。"""

    patch: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    by: str = "local"


class ResetRequest(BaseModel):
    """恢复默认：给 keys 只清这几项，all=true 清全部。"""

    keys: list[str] = Field(default_factory=list)
    all: bool = False
    by: str = "local"


def _spec_of(domain: str) -> DomainSpec:
    spec = BY_KEY.get(domain)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知配置域 {domain}")
    return spec


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "config_invalid", "message": message})


def _payload(snapshot: store.Snapshot, spec: DomainSpec) -> dict[str, Any]:
    locked = set(env_overlay(spec.key))
    return {
        "domain": spec.key,
        "title": spec.title,
        "values": snapshot.values,
        "defaults": snapshot.defaults,
        "sources": snapshot.sources,
        "bounds": config_store.bounds(spec.key),
        "editable": sorted(_editable(snapshot, locked)) if spec.writable else [],
        "env_locked": sorted(locked),
        "writable": spec.writable,
        "managed_by": spec.managed_by,
        "hot": spec.hot,
        "note": spec.note,
        "version": snapshot.version,
        "drifted": snapshot.drifted,
        "mode": config_store.mode(),
        "db_path": str(config_store.db_path()),
    }


def _editable(snapshot: store.Snapshot, locked: set[str]) -> list[str]:
    return [key for key in store.flatten(snapshot.values) if key not in locked]


@router.get("/api/config")
def list_domains() -> dict[str, Any]:
    """面板首页：域清单 + 全局存储位置（前端据此显示「写到哪里」）。"""
    return {
        "mode": config_store.mode(),
        "db_path": str(config_store.db_path()),
        "domains": config_store.index(),
    }


@router.get("/api/config/{domain}")
def read_domain(domain: str) -> dict[str, Any]:
    """生效值 / 默认值 / 每键来源 / 边界 / 生效时机。"""
    spec = _spec_of(domain)
    try:
        return _payload(config_store.resolved(domain), spec)
    except store.InvalidConfig as exc:
        raise _bad(str(exc)) from exc


@router.patch("/api/config/{domain}")
def patch_domain(domain: str, body: PatchRequest) -> dict[str, Any]:
    """增量写入：`null` 删覆盖，其余落库并 bump 版本。"""
    spec = _spec_of(domain)
    try:
        snapshot = config_store.patch(domain, body.patch, expected=body.version, by=body.by)
    except store.ConfigConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "config_version_conflict", **exc.payload},
        ) from exc
    except store.InvalidConfig as exc:
        raise _bad(str(exc)) from exc
    return {**_payload(snapshot, spec), "persisted": snapshot.version > 0}


@router.post("/api/config/{domain}/reset")
def reset_domain(domain: str, body: ResetRequest) -> dict[str, Any]:
    """恢复默认：删掉覆盖行（不是把当前值写成默认值）。"""
    spec = _spec_of(domain)
    if not body.keys and not body.all:
        raise _bad("reset 需要 keys 或 all=true：空请求会被误读成「什么都没做」")
    try:
        snapshot = config_store.reset(domain, body.keys, all_=body.all, by=body.by)
    except store.InvalidConfig as exc:
        raise _bad(str(exc)) from exc
    return _payload(snapshot, spec)


@router.get("/api/config/{domain}/export", response_class=PlainTextResponse)
def export_domain(domain: str) -> str:
    """导出覆盖项 yaml：可进 PR 评审，也可当另一台机器的导入源。"""
    _spec_of(domain)
    return config_store.export_yaml(domain)


@router.get("/api/config/{domain}/audit")
def audit_domain(domain: str, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """最近的改动：谁在什么时候把哪个键从什么改成了什么。"""
    _spec_of(domain)
    return {"domain": domain, "entries": config_store.audit(domain, limit)}
