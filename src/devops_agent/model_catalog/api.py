"""供应商与模型列表端点（SPEC-0018 §8；网关写入口见 SPEC-0022 §6）。

四个刻意的形状约束：

1. **只回 host，不回完整 URL** —— URL 里可能带 token 查询参数。
2. **只回 `key_fingerprint`，不回 key 也不回尾号** —— 尾号也是信息。
3. **不提供 `/v1/models`** —— 本服务不是模型网关，挂兼容路径会让人以为能改写
   模型或做多租户分流，而那些都不支持。
4. **写入口只改 `providers` / `default`**，其余 yaml 键原样保留，注释按模板重建。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from devops_agent.model_catalog import ProviderError, admin, discover, get_catalog
from devops_agent.model_catalog.aggregate import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    AggregateUnavailable,
    aggregate,
)

router = APIRouter()

#: 单网关列表页上限：网关可能返回几百条，面板一次只画一屏。
_PAGE_MAX = 500


class ProviderBody(BaseModel):
    """界面提交的一条网关。`api_key_ref` 只收环境变量名或 `none`。"""

    id: str = ""
    kind: str = ""
    label: str = ""
    base_url: str = ""
    api_key_ref: str = ""
    discover: str = "models"


class ProviderWrite(BaseModel):
    """写请求：`expected_version` 是内容摘要，冲突回 409。"""

    expected_version: str | None = None
    provider: ProviderBody
    default: str | None = None


def _provider_error(exc: ProviderError) -> HTTPException:
    catalog = get_catalog()
    try:
        available = catalog.registry.ids()
    except ProviderError:
        available = []
    return HTTPException(
        status_code=404,
        detail={"code": "unknown_provider", "reason": exc.reason, "available": available},
    )


def _admin_error(exc: admin.AdminError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail={"code": exc.code, "reason": exc.reason, **exc.extra},
    )


def _clamp(limit: int) -> int:
    return max(1, min(_PAGE_MAX, limit))


def _payload() -> dict[str, Any]:
    """读清单 + 内容版本：界面据此做乐观锁，不必自己算摘要。"""
    raw = admin.read_raw()
    body = get_catalog().providers_payload()
    body["version"] = admin.version_of(raw)
    return body


@router.get("/api/providers")
def list_providers() -> dict[str, object]:
    """已配置网关清单（脱敏）。打开面板只发这一个请求，不打上游。"""
    try:
        return _payload()
    except admin.AdminError as exc:
        raise _admin_error(exc) from exc
    except ProviderError as exc:
        # 注册表坏了：回 500 而不是空列表 —— 空列表会被当成「没配网关」
        raise HTTPException(
            status_code=500,
            detail={"code": "providers_invalid", "field": exc.field, "reason": exc.reason},
        ) from exc


@router.post("/api/providers", status_code=201)
def create_provider(req: ProviderWrite) -> dict[str, Any]:
    """新增网关。id 唯一；`default` 省略时把新网关设为默认（首次配置的场景）。"""
    try:
        version = admin.create(
            req.provider.model_dump(), expected=req.expected_version, default=req.default
        )
    except admin.AdminError as exc:
        raise _admin_error(exc) from exc
    except ProviderError as exc:
        raise HTTPException(400, {"code": "providers_invalid", "reason": exc.reason}) from exc
    return {"version": version, "providers": admin.summarize(admin.read_raw())}


@router.patch("/api/providers/{provider_id}")
def update_provider(provider_id: str, req: ProviderWrite) -> dict[str, Any]:
    """改网关。`id` 不可改：改名等于删了重建，会让会话里的 provider 失效。"""
    try:
        version = admin.update(
            provider_id,
            req.provider.model_dump(),
            expected=req.expected_version,
            default=req.default,
        )
    except admin.AdminError as exc:
        raise _admin_error(exc) from exc
    except ProviderError as exc:
        raise HTTPException(400, {"code": "providers_invalid", "reason": exc.reason}) from exc
    return {"version": version, "providers": admin.summarize(admin.read_raw())}


@router.delete("/api/providers/{provider_id}")
def delete_provider(
    provider_id: str,
    expected_version: str = Query(..., min_length=4),
    reassign_to: str | None = None,
) -> dict[str, Any]:
    """删网关。删的是默认网关时必须带 `reassign_to`，否则会话会指向一个不存在的家。"""
    try:
        version = admin.delete(provider_id, expected=expected_version, reassign_to=reassign_to)
    except admin.AdminError as exc:
        raise _admin_error(exc) from exc
    except ProviderError as exc:
        raise HTTPException(400, {"code": "providers_invalid", "reason": exc.reason}) from exc
    return {"version": version, "providers": admin.summarize(admin.read_raw())}


@router.post("/api/providers/{provider_id}/check")
async def check_provider(provider_id: str) -> dict[str, object]:
    """探活：拉一次 `/models`，只回状态与耗时，不写文件、不改缓存里的 key。"""
    try:
        spec = get_catalog().registry.get(provider_id)
    except ProviderError as exc:
        raise _provider_error(exc) from exc
    if spec.discover != "models":
        # 人工清单 / 离线卡没有上游可打：探活等于「本地清单在不在」
        return {
            "id": spec.id,
            "code": "ok",
            "http_status": None,
            "total": len(get_catalog().known_models(spec.id)),
            "latency_ms": 0,
        }
    started = time.perf_counter()
    result = await discover(spec, force=True)
    return {
        "id": spec.id,
        "code": result.code,
        "http_status": result.http_status,
        "total": len(result.items),
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


@router.get("/api/models")
async def all_models(
    q: str = "",
    provider: str = "",
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    refresh: bool = False,
) -> dict[str, object]:
    """跨网关聚合：会话里的模型选择器只发这一个请求。"""
    try:
        return await aggregate(
            get_catalog(), query=q, provider=provider, limit=limit, refresh=refresh
        )
    except AggregateUnavailable as exc:
        raise HTTPException(
            status_code=503, detail={"code": "models_unavailable", "reason": str(exc)}
        ) from exc
    except ProviderError as exc:
        raise _provider_error(exc) from exc


@router.get("/api/providers/{provider_id}/draft")
def provider_draft(provider_id: str) -> dict[str, object]:
    """编辑用的一条完整配置（地址与 key 变量名）。

    为什么不并进清单：清单这一屏常被截屏，只该有 host；而编辑页必须能改端口，
    否则用户只能重敲一遍。`safe_url` 已剥掉 query/fragment（可能挂 token）。
    """
    try:
        spec = get_catalog().registry.get(provider_id)
    except ProviderError as exc:
        raise _provider_error(exc) from exc
    return {
        "id": spec.id,
        "kind": spec.kind,
        "label": spec.display,
        "base_url": spec.safe_url,
        "api_key_ref": spec.key_env or ("none" if spec.api_key == "none" else ""),
        "discover": spec.discover,
    }


@router.get("/api/providers/{provider_id}/models")
async def list_models(
    provider_id: str,
    q: str = "",
    limit: int = Query(default=200, ge=1, le=_PAGE_MAX),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """发现的模型列表（缓存优先）。`q` / `limit` / `offset` 支持大网关。"""
    try:
        return await get_catalog().models(provider_id, query=q, limit=_clamp(limit), offset=offset)
    except ProviderError as exc:
        raise _provider_error(exc) from exc


@router.post("/api/providers/{provider_id}/models/refresh")
async def refresh_models(provider_id: str) -> dict[str, object]:
    """强制绕过缓存（用户点了刷新）。失败也回 200：`code` 说明失败原因。"""
    try:
        return await get_catalog().refresh(provider_id)
    except ProviderError as exc:
        raise _provider_error(exc) from exc


@router.get("/api/providers/{provider_id}/models/{model_id:path}")
def model_card(provider_id: str, model_id: str) -> dict[str, object]:
    """单模型能力卡。`{model_id:path}` 是必须的：Gemini 是 `models/gemini-2.5-pro`，
    OpenRouter 是 `anthropic/claude-sonnet-4`，普通路径参数会在 `/` 处截断。
    """
    try:
        return get_catalog().card(provider_id, model_id)
    except ProviderError as exc:
        raise _provider_error(exc) from exc


__all__ = ["router"]
