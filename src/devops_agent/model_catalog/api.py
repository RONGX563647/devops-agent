"""供应商与模型列表端点（SPEC-0018 §8）。

三个刻意的形状约束：

1. **只回 host，不回完整 URL** —— URL 里可能带 token 查询参数。
2. **只回 `key_fingerprint`，不回 key 也不回尾号** —— 尾号也是信息。
3. **不提供 `/v1/models`** —— 本服务不是模型网关，挂兼容路径会让人以为能改写
   模型或做多租户分流，而那些都不支持。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from devops_agent.model_catalog import ProviderError, get_catalog

router = APIRouter()

#: 列表页上限：网关可能返回几百条，面板一次只画一屏。
MAX_LIMIT = 500


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


def _clamp(limit: int) -> int:
    return max(1, min(MAX_LIMIT, limit))


@router.get("/api/providers")
def list_providers() -> dict[str, object]:
    """已配置网关清单（脱敏）。打开抽屉只发这一个请求，不打上游。"""
    try:
        return get_catalog().providers_payload()
    except ProviderError as exc:
        # 注册表坏了：回 500 而不是空列表 —— 空列表会被当成「没配网关」
        raise HTTPException(
            status_code=500,
            detail={"code": "providers_invalid", "field": exc.field, "reason": exc.reason},
        ) from exc


@router.get("/api/providers/{provider_id}/models")
async def list_models(
    provider_id: str,
    q: str = "",
    limit: int = Query(default=200, ge=1, le=MAX_LIMIT),
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
