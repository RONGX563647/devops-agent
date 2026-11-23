"""密钥 API —— 只读元信息与删除，永不返回密文或明文。

「能不能看到明文」是一个刻意的取舍：接口层不给读回通道，
即使前端被 XSS 也拿不到已入库的凭据；要用明文只有工具执行那一条路。
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


@router.delete("/secrets/{name}", response_model=DeleteSecretOut)
def delete_secret(name: str) -> DeleteSecretOut:
    """删除一条凭据；删除后遗留的占位符会原样透传，不再被注入。"""
    return DeleteSecretOut(deleted=get_vault().delete(name))
