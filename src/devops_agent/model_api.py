"""模型参数配置端点 —— 部署默认 / 会话覆盖 / 只读凭据（SPEC-0001）。

为什么单独一个模块而不是塞进网关：网关已经承担 SSE 收尾、挂起恢复、
权限切档三件事，再把十几个参数校验放进去，任何一处改动都要重读整个文件。

三条硬约定：

1. **服务端是唯一裁判**。前端的禁用/提示只是提前告知，越界一律 400，
   附 `{field, reason, bounds}` 让界面能高亮具体那一行。
2. **`null` 表示重置为下层默认**，不是「把值设成 null」。否则「温度改回默认」
   和「温度设为 0」在界面上无法区分。
3. **不谎报生效范围**。没有活跃实例、或运行时不支持热替换时返回 `applied=false`，
   `effective_from` 说「下一轮」——沿用切档端点已经建立的约定。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from devops_agent import model_settings
from devops_agent.model_settings import InvalidConfig
from devops_agent.observability import repository as repo
from devops_agent.observability.recorder import utc_now
from devops_agent.runtime.sessions import registry_for

router = APIRouter()

# 缺省域：前端传 domain 时以它为准（max_iters 按域取值）
DEFAULT_DOMAIN = "devops"


class PatchRequest(BaseModel):
    """部署默认的增量修改。`version` 省略表示不校验并发（脚本/首次写入）。"""

    patch: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None


class ResetRequest(BaseModel):
    """恢复部署默认：给 keys 只清这几项，all=true 清全部。"""

    keys: list[str] = Field(default_factory=list)
    all: bool = False


class SessionModelRequest(BaseModel):
    """会话级覆盖（合并语义，不是替换）。"""

    overrides: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


# --- 内部：读写与校验 ---------------------------------------------------


def _stored_now() -> tuple[dict[str, Any], int]:
    """直接从库里读一份新的部署默认（不吃 TTL 缓存）：写入前必须看到别人改过的值。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        saved = repo.load_setting(conn, model_settings.SETTING_KEY)
    return saved if saved is not None else ({}, 0)


def _invalid(exc: InvalidConfig, *, remedy: str | None = None) -> HTTPException:
    """把校验失败翻译成前端可高亮的 400。`remedy` 附在 reason 后给出自救动作。"""
    reason = exc.reason if remedy is None else f"{exc.reason}；{remedy}"
    return HTTPException(
        status_code=400,
        detail={
            "code": "invalid_model_config",
            "field": exc.field,
            "reason": reason,
            "bounds": model_settings.bound_payload(exc.field),
        },
    )


def _conflict(version: int, resolved: dict[str, Any]) -> HTTPException:
    """乐观并发失败：带回当前版本与现值，前端让用户选「覆盖」还是「放弃」。"""
    return HTTPException(
        status_code=409,
        detail={"code": "config_version_conflict", **model_settings.payload(resolved, version)},
    )


def _checked_values(
    stored_layer: dict[str, Any],
    session_layer: dict[str, Any] | None = None,
    *,
    domain: str = DEFAULT_DOMAIN,
    remedy: str | None = None,
) -> dict[str, Any]:
    """解析 + 校验，返回完整参数值；非法时抛 400。"""
    resolved = model_settings.resolve(session_layer, stored_layer, domain=domain)
    values = model_settings.values_of(resolved)
    try:
        model_settings.validate(values)
    except InvalidConfig as exc:
        raise _invalid(exc, remedy=remedy) from None
    return values


def _body_payload(
    stored_layer: dict[str, Any],
    version: int,
    session_layer: dict[str, Any] | None = None,
    *,
    domain: str = DEFAULT_DOMAIN,
) -> dict[str, Any]:
    """GET / PATCH / reset 共用的响应体。"""
    resolved = model_settings.resolve(session_layer, stored_layer, domain=domain)
    return model_settings.payload(resolved, version)


def _save_stored(merged: dict[str, Any], *, expected: int) -> int:
    """写回部署默认；版本不匹配时抛 409。"""
    with repo.connect() as conn:
        repo.init_db(conn)
        saved = repo.save_setting(
            conn,
            model_settings.SETTING_KEY,
            merged,
            expected_version=expected,
            updated_at=utc_now(),
        )
    if saved is None:
        current, version = _stored_now()
        # 冲突说明本进程缓存里的版本已过时（别的进程写过）。必须先失效再返回 409，
        # 否则前端拿新版本重试时，本进程仍读旧缓存，可能陷入反复冲突直到 TTL 过期。
        model_settings.invalidate()
        raise _conflict(version, model_settings.resolve(stored=current))
    model_settings.invalidate()
    return saved


# --- 部署默认 -----------------------------------------------------------


@router.get("/api/model-config")
def read_model_config(session: str | None = None, domain: str = DEFAULT_DOMAIN) -> dict[str, Any]:
    """当前生效参数 + 逐项来源 + 边界 + 派生值。

    `session` 给了就把该会话的覆盖一起算进来（前端面板打开时用）。
    """
    stored, version = _stored_now()
    overrides = _session_overrides(session) if session else {}
    body = _body_payload(stored, version, overrides, domain=domain)
    if session:
        body["overrides"] = overrides
    return body


@router.patch("/api/model-config")
def patch_model_config(req: PatchRequest, request: Request) -> dict[str, Any]:
    """改部署默认：写 DB → 让活跃会话热替换（跑着的记 deferred）。"""
    stored, version = _stored_now()
    if req.version is not None and req.version != version:
        # 与 _save_stored 的冲突分支同理：版本对不上说明本进程缓存已过时，
        # 不失效的话紧接着的重试仍会读到旧版本。
        model_settings.invalidate()
        raise _conflict(version, model_settings.resolve(stored=stored))
    try:
        merged = model_settings.apply_patch(stored, req.patch)
    except InvalidConfig as exc:
        raise _invalid(exc) from None
    _checked_values(merged)
    new_version = _save_stored(merged, expected=version)
    affected = registry_for(request).broadcast_model_defaults()
    return {**_body_payload(merged, new_version), "affected": affected}


@router.post("/api/model-config/reset")
def reset_model_config(req: ResetRequest, request: Request) -> dict[str, Any]:
    """清除部署默认里的覆盖项，回落到 yaml / env / 框架默认。"""
    stored, version = _stored_now()
    merged = {} if req.all else {k: v for k, v in stored.items() if k not in set(req.keys)}
    # reset 也是写路径：库里若被手工写入非法值（运维改库 / 旧版本遗留），
    # 删掉别的键后原样返回给前端就等于绕过校验。服务端是唯一裁判，非法照样 400。
    _checked_values(merged, remedy='可用 {"all": true} 清空全部覆盖项以恢复默认')
    new_version = _save_stored(merged, expected=version)
    affected = registry_for(request).broadcast_model_defaults()
    return {**_body_payload(merged, new_version), "affected": affected}


# --- 会话覆盖 -----------------------------------------------------------


def _session_overrides(session_id: str) -> dict[str, Any]:
    with repo.connect() as conn:
        repo.init_db(conn)
        return repo.load_session_model_config(conn, session_id)


def _save_overrides(session_id: str, overrides: dict[str, Any]) -> None:
    with repo.connect() as conn:
        repo.init_db(conn)
        repo.save_session_model_config(conn, session_id, overrides, updated_at=utc_now())


def _check_session(session_id: str, body_id: str | None) -> None:
    """路径与 body 的会话 id 必须一致：对齐切档端点的约定，防止改错会话。"""
    if body_id is not None and body_id != session_id:
        raise HTTPException(status_code=400, detail="session_mismatch")


@router.get("/api/session/{session_id}/model-config")
def read_session_model_config(session_id: str) -> dict[str, Any]:
    """该会话的生效参数：会话覆盖 > 部署默认 > yaml > env。"""
    stored, version = _stored_now()
    overrides = _session_overrides(session_id)
    return {**_body_payload(stored, version, overrides), "overrides": overrides}


@router.patch("/api/session/{session_id}/model-config")
def patch_session_model_config(
    session_id: str, req: SessionModelRequest, request: Request
) -> dict[str, Any]:
    """改会话级覆盖（合并语义）。运行中也能改：原地热替换，不打断当前流。"""
    _check_session(session_id, req.session_id)
    stored, version = _stored_now()
    overrides = _session_overrides(session_id)
    try:
        merged = model_settings.apply_patch(overrides, req.overrides)
    except InvalidConfig as exc:
        raise _invalid(exc) from None
    _checked_values(stored, merged)
    _save_overrides(session_id, merged)
    applied = registry_for(request).apply_model_params(session_id, merged)
    return {
        **_body_payload(stored, version, merged),
        "overrides": merged,
        "applied": applied,
        "effective_from": "下一次模型调用" if applied else "下一轮（当前没有活跃会话）",
    }


@router.delete("/api/session/{session_id}/model-config")
def clear_session_model_config(session_id: str, request: Request) -> dict[str, Any]:
    """清空该会话的全部覆盖，回到部署默认。"""
    stored, version = _stored_now()
    _save_overrides(session_id, {})
    applied = registry_for(request).apply_model_params(session_id, {})
    return {
        **_body_payload(stored, version),
        "overrides": {},
        "cleared": True,
        "applied": applied,
        "effective_from": "下一次模型调用" if applied else "下一轮（当前没有活跃会话）",
    }
