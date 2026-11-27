"""逐项解析「值 + 来源」，以及部署默认的进程内缓存。"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from devops_agent.config import ModelSettings, load_model
from devops_agent.config_store import health as config_health
from devops_agent.model_settings import file_store
from devops_agent.model_settings.bounds import EDITABLE
from devops_agent.observability import repository as repo

logger = logging.getLogger(__name__)

MODEL_ENV = "DEVOPS_AGENT_MODEL"
# settings 表里这份配置的键名：一张 KV 表装所有「前端可改的配置域」
SETTING_KEY = "model_config"
# 进程内缓存 TTL：配置写进 DB 后可能被另一个进程读到，5 秒足够让单 worker 外的
# 场景在下一轮收敛，又不会让每次读参数都打一次 SQLite。
# 不加 Redis / pub-sub：这个规模不需要第二套通知机制。
STORED_TTL_S = 5.0


class Source(StrEnum):
    """一个参数值是从哪一层来的。前端按它画徽标。"""

    SESSION = "session"
    STORED = "stored"
    YAML = "yaml"
    ENV = "env"
    FRAMEWORK = "framework-default"


@dataclass(frozen=True)
class Resolved:
    """解析结果：值 + 来源。来源不是装饰，它是「缺陷一为什么藏了这么久」的答案。"""

    value: Any
    source: Source


def _model_name(settings: ModelSettings, declared: set[str]) -> Resolved:
    """模型名：yaml 显式写了就认 yaml，否则环境变量，都没有就空（Echo 场景）。"""
    from_yaml = bool(settings.name) and "name" in declared
    if from_yaml:
        return Resolved(settings.name, Source.YAML)
    env_name = os.environ.get(MODEL_ENV, "")
    if env_name:
        return Resolved(env_name, Source.ENV)
    return Resolved(env_name, Source.FRAMEWORK)


def _scalar(settings: ModelSettings, declared: set[str], key: str, value: Any) -> Resolved:
    """yaml 里显式声明过就是 yaml，否则是框架默认 —— 这是「未校准」的判据。"""
    return Resolved(value, Source.YAML if key in declared else Source.FRAMEWORK)


def _base(domain: str) -> dict[str, Resolved]:
    """yaml + env + 框架默认三层合成的地板值。"""
    settings = load_model()
    declared = set(settings.model_fields_set)
    values: dict[str, Resolved] = {
        "model": _model_name(settings, declared),
        "max_iters": _scalar(settings, declared, "max_iters", settings.max_iters_for(domain)),
    }
    for key in EDITABLE:
        if key in values:
            continue
        values[key] = _scalar(settings, declared, key, getattr(settings, key))
    return values


def _apply_layer(values: dict[str, Resolved], layer: dict[str, Any] | None, source: Source) -> None:
    """把一层覆盖叠上去。null 表示「该项回落下层」，所以跳过而不是写入。"""
    for key, value in (layer or {}).items():
        if key in values and value is not None:
            values[key] = Resolved(value, source)


def resolve(
    session: dict[str, Any] | None = None,
    stored: dict[str, Any] | None = None,
    *,
    domain: str = "devops",
) -> dict[str, Resolved]:
    """逐项解析「值 + 来源」。纯函数：三份 dict 进，一份 dict 出，不碰 DB。"""
    values = _base(domain)
    _apply_layer(values, stored, Source.STORED)
    _apply_layer(values, session, Source.SESSION)
    return values


def values_of(resolved: dict[str, Resolved]) -> dict[str, Any]:
    """去掉来源，只留值。构造模型 / 校验 / 写 run 记录都用这份。"""
    return {key: item.value for key, item in resolved.items()}


# --- 部署默认（DB）的进程内缓存 ----------------------------------------

_stored_cache: tuple[float, dict[str, Any], int] | None = None


def invalidate() -> None:
    """写入部署默认后清缓存：本进程立刻看到自己的改动，别的进程靠 TTL 收敛。"""
    global _stored_cache
    _stored_cache = None


def _read_stored() -> tuple[dict[str, Any], int]:
    """从 settings 表读部署默认；库不可用时返回空配置（回落 yaml/env）。

    `except Exception` 保留（存储故障不该挡住一次对话），但不再静默：与
    `config_store` 共用一份降级计数，`/health` 的 `config.degraded` 会亮起来。
    旧实现的三种失败语义各说各话（500 / 静默 / 报错），排障时根本分不清是
    「没配」还是「读不到」。
    """
    try:
        with repo.connect() as conn:
            repo.init_db(conn)
            saved = repo.load_setting(conn, SETTING_KEY)
    except Exception as exc:  # noqa: BLE001 - 存储故障不该挡住一次对话
        config_health.note_failure("model", f"{type(exc).__name__}: {exc}")
        logger.warning("模型参数库不可读，回落 config/model*.yaml 与环境变量", exc_info=True)
        return {}, 0
    return saved if saved is not None else ({}, 0)


def stored_defaults(*, force: bool = False) -> dict[str, Any]:
    """部署默认参数：DB 旧值叠加 `model.local.yaml`，文件优先。

    文件是真相、DB 是账本：这样手改文件立即生效，而老部署里只存在于 DB
    的值也不会凭空消失（下一次写入会把它一起落盘）。
    """
    db, _ = _read_stored_cached(force=force)
    return {**db, **file_store.load()}


def _read_stored_cached(*, force: bool = False) -> tuple[dict[str, Any], int]:
    """DB 层带 TTL 缓存；文件层自己按 mtime 缓存，两者互不影响。"""
    global _stored_cache
    now = time.monotonic()
    if not force and _stored_cache is not None and now - _stored_cache[0] < STORED_TTL_S:
        return _stored_cache[1], _stored_cache[2]
    value, version = _read_stored()
    _stored_cache = (now, value, version)
    return value, version


def stored_version() -> int:
    """部署默认的版本号，供 GET 与乐观并发使用。"""
    _, version = _read_stored_cached()
    return version


def stored_db(*, force: bool = False) -> dict[str, Any]:
    """只看 DB 层（layers 面板用来区分「文件」和「历史库值」）。"""
    value, _ = _read_stored_cached(force=force)
    return value


def layers(
    session: dict[str, Any] | None = None,
    *,
    domain: str = "devops",
) -> list[dict[str, Any]]:
    """四层原始值，供前端画「这个值到底从哪来」的链路。

    顺序即优先级（高 → 低）；`writable=false` 的层只能改文件或环境变量，
    前端据此把「在文件中修改」的提示指向具体路径。
    """
    base = _base(domain)
    return [
        {"id": "session", "label": "本次会话", "writable": True, "values": dict(session or {})},
        {
            "id": "file",
            "label": "config/model.local.yaml",
            "writable": True,
            "path": str(file_store.local_path()),
            "values": file_store.load(),
        },
        {
            "id": "stored",
            "label": "数据库旧值（迁移残留）",
            "writable": True,
            "values": stored_db(),
        },
        {
            "id": "base",
            "label": "config/model.yaml / 环境变量 / 框架默认",
            "writable": False,
            "values": values_of(base),
        },
    ]


def sources_of(resolved: dict[str, Resolved]) -> dict[str, str]:
    """只看来源。前端徽标与 `sources` 响应是同一份数据。"""
    return {key: item.source.value for key, item in resolved.items()}
