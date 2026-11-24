"""密钥 API —— 元信息、删除，以及一条受控的明文读回通道。

读回的取舍：工具输出里捕获的凭据（source=output）若只进不回，用户就永远
看不到工具到底读到了什么。因此提供 `GET /api/secrets/{name}/value` 返回明文，
但每次读回都走 `get()` 记一次 uses 留痕。本服务默认只绑 127.0.0.1、无认证，
这条通道与「删除」同级——部署到非本机时必须先加认证/网络边界。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from devops_agent.secrets.vault import SecretMeta, get_vault

router = APIRouter(prefix="/api", tags=["secrets"])


class SecretOut(BaseModel):
    """一条凭据的元信息：名字、提示、来源、使用情况。"""

    name: str
    hint: str
    source: str
    created_at: str
    last_used_at: str | None
    uses: int


class DeleteSecretOut(BaseModel):
    deleted: bool


class SecretValueOut(BaseModel):
    """一次明文读回：值 + 是否命中。未命中时 value 为空。"""

    name: str
    value: str
    found: bool


def _to_out(meta: SecretMeta) -> SecretOut:
    return SecretOut(
        name=meta.name,
        hint=meta.hint,
        source=meta.source,
        created_at=meta.created_at,
        last_used_at=meta.last_used_at,
        uses=meta.uses,
    )


@router.get("/secrets", response_model=list[SecretOut])
def list_secrets() -> list[SecretOut]:
    """列出保险箱里的凭据（不含值）。"""
    return [_to_out(meta) for meta in get_vault().list()]


@router.get("/secrets/{name}/value", response_model=SecretValueOut)
def read_secret_value(name: str) -> SecretValueOut:
    """受控读回明文：每次都经 get() 记一次使用，便于事后审计谁看过。

    解不开/不存在返回 found=false（不区分原因，避免探测库里有哪些名字）。
    """
    value = get_vault().get(name)
    return SecretValueOut(name=name, value=value or "", found=value is not None)


@router.delete("/secrets/{name}", response_model=DeleteSecretOut)
def delete_secret(name: str) -> DeleteSecretOut:
    """删除一条凭据；删除后遗留的占位符会原样透传，不再被注入。"""
    return DeleteSecretOut(deleted=get_vault().delete(name))
