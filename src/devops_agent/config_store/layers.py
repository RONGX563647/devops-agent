"""四层合流与快照 —— 读路径的全部逻辑，不碰写入。

分层的顺序是**固定的**，改顺序就是改语义：

    代码默认（pydantic 字段默认） ← yaml 基线 ← config.db 覆盖 ← 环境变量

每次解析都从代码默认重建，而不是在旧结果上打补丁：这样删掉一条覆盖不会留下
「空洞」，某个键的来源归属也不会因缓存陈旧而漂移。

读失败一律降级为空层（配置坏了不该拦住对话），但会记进 `config_store.health`，
并由 `/health` 暴露 —— 降级可以，装作没事不行。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from devops_agent.config_files import read_yaml
from devops_agent.config_store import health, schema
from devops_agent.config_store.errors import InvalidConfig
from devops_agent.config_store.registry import (
    DomainSpec,
    defaults_of,
    env_overlay,
    model_for,
    spec_of,
    yaml_segment,
)
from devops_agent.config_store.structure import flatten, merge, unflatten

logger = logging.getLogger(__name__)

# 多进程下靠它感知别人写入：一次 TTL 之后重读版本号与覆盖行。
TTL_S = 2.0


@dataclass(slots=True)
class Snapshot:
    """一次解析的完整结果：生效值、默认值、来源、版本、漂移键、本次是否落库。"""

    domain: str
    values: dict[str, Any]
    defaults: dict[str, Any]
    sources: dict[str, str]
    version: int
    drifted: list[str] = field(default_factory=list)
    # 只对写操作有意义：本次是否真的改了库（`patch` 写成同值时为 False）
    persisted: bool = False


@dataclass(slots=True)
class DbLayer:
    """该域的覆盖行：值、写入时的默认值、版本号。"""

    overrides: dict[str, Any]
    at_write: dict[str, str]
    version: int


def _explain(error: ValidationError) -> str:
    """把 pydantic 的第一条错误说成「哪个键、错在哪」，界面据此高亮。"""
    first = error.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "配置"
    return f"{loc}: {first.get('msg', '取值不合法')}"


def yaml_layer(spec: DomainSpec) -> dict[str, Any]:
    """仓库基线：`config/<section>.yaml` 里该域的值；缺失即空。

    `root=True` 的域（policy.yaml）整份文件就是负载；其余取与文件名同名的段。
    不再用「找不到段就当整份文件」的兜底 —— 那会让「文件顶层多一个键」这类
    编辑事故整段静默失效。
    """
    raw = read_yaml(f"{spec.section}.yaml")
    segment = yaml_segment(spec)
    if segment is None:
        return raw
    section = raw.get(segment)
    return section if isinstance(section, dict) else {}


def floor_flat(spec: DomainSpec) -> dict[str, Any]:
    """下层合流后的每个键（默认 ← yaml ← env），用来判断「这个值算不算覆盖」。"""
    merged = merge(defaults_of(spec), yaml_layer(spec))
    merged = merge(merged, unflatten(env_overlay(spec.key)))
    return flatten(merged)


def read_layer(conn: sqlite3.Connection, domain: str) -> DbLayer:
    """在给定连接上读该域的覆盖行（写事务里复用同一个连接）。"""
    rows = conn.execute(
        "SELECT key, value, default_at_write FROM config_entries WHERE domain=?",
        (domain,),
    ).fetchall()
    row = conn.execute("SELECT version FROM config_domains WHERE domain=?", (domain,)).fetchone()
    overrides: dict[str, Any] = {}
    at_write: dict[str, str] = {}
    for item in rows:
        try:
            overrides[str(item["key"])] = json.loads(str(item["value"]))
        except json.JSONDecodeError:
            logger.warning("配置 %s.%s 不是合法 JSON，忽略该行", domain, item["key"])
            continue
        at_write[str(item["key"])] = str(item["default_at_write"] or "")
    return DbLayer(overrides, at_write, int(row["version"]) if row else 0)


def _empty(domain: str) -> DbLayer:
    return DbLayer({}, {}, 0)


def db_layers(domains: list[str]) -> dict[str, DbLayer]:
    """一次连接读多个域：面板首页不必为每个域各开一次连接、各跑一遍 DDL。

    读失败（文件被删、被截断、权限变化）时全部按空层处理并记健康 —— 这是读路径，
    不能因为配置库坏了就拒绝服务。
    """
    if not schema.db_ready():
        health.note_success("db")
        return {domain: _empty(domain) for domain in domains}
    try:
        with schema.connect() as conn:
            layers = {domain: read_layer(conn, domain) for domain in domains}
    except (OSError, sqlite3.Error) as exc:
        health.note_failure("db", f"{type(exc).__name__}: {exc}")
        # 不打完整堆栈：读路径每 TTL 一次，坏库会刷屏；明细在 /health 的 error 里
        logger.warning("配置库不可读，按无覆盖处理：%s: %s", type(exc).__name__, exc)
        return {domain: _empty(domain) for domain in domains}
    health.note_success("db")
    return layers


def db_layer(domain: str) -> DbLayer:
    """读单个域的覆盖行；`file` 模式或库不存在时返回空层。"""
    return db_layers([domain])[domain]


def build(spec: DomainSpec, layer: DbLayer) -> Snapshot:
    """把四层合成一份校验过的快照。未知键会被模型丢弃，来源只按生效值统计。"""
    defaults = defaults_of(spec)
    merged = merge(defaults, yaml_layer(spec))
    merged = merge(merged, unflatten(layer.overrides))
    # env 最高：容器注入的值必须压过库里的值，否则界面显示的和进程跑的不是一回事
    merged = merge(merged, unflatten(env_overlay(spec.key)))
    try:
        values = model_for(spec).model_validate(merged).model_dump()
    except ValidationError as exc:
        raise InvalidConfig(_explain(exc)) from exc
    # 来源只认「这一层自己有没有这个键」：yaml 层用自身键集，不能拿合并后的
    # floor 去判 —— defaults 里的键全在 floor 中，否则 "default" 永远不可达，
    # 界面会把所有代码默认值谎报成 yaml 基线（删掉 yaml 后尤其误导）。
    yaml_keys = flatten(yaml_layer(spec))
    env = flatten(env_overlay(spec.key))
    sources: dict[str, str] = {}
    for key in flatten(values):
        if key in env:
            sources[key] = "env"
        elif key in layer.overrides:
            sources[key] = "db"
        elif key in yaml_keys:
            sources[key] = "yaml"
        else:
            sources[key] = "default"
    return Snapshot(
        spec.key,
        values,
        defaults,
        sources,
        layer.version,
        drifted(defaults, layer),
    )


def drifted(defaults: dict[str, Any], layer: DbLayer) -> list[str]:
    """覆盖写入时的默认值与现在不同 → 代码默认升级过，但旧覆盖仍压在上面。"""
    current = flatten(defaults)
    out: list[str] = []
    for key, recorded in layer.at_write.items():
        if not recorded:
            continue
        try:
            before = json.loads(recorded)
        except json.JSONDecodeError:
            continue
        if key in current and current[key] != before:
            out.append(key)
    return sorted(out)


# --- 对外读取（带 TTL 缓存）---------------------------------------------

_cache: dict[str, tuple[float, Snapshot]] = {}


def resolved(domain: str) -> Snapshot:
    """带 TTL 缓存的解析结果。"""
    spec = spec_of(domain)
    cached = _cache.get(domain)
    now = time.monotonic()
    if cached is not None and now - cached[0] < TTL_S:
        return cached[1]
    snapshot = build(spec, db_layer(domain))
    _cache[domain] = (now, snapshot)
    return snapshot


def effective(domain: str) -> dict[str, Any]:
    """给加载器用的嵌套 dict（已校验、已合流）。"""
    return resolved(domain).values


def invalidate(domain: str | None = None) -> None:
    """写后失效；测试切换配置目录 / 库路径时也用它。"""
    if domain is None:
        _cache.clear()
    else:
        _cache.pop(domain, None)


__all__ = [
    "TTL_S",
    "DbLayer",
    "Snapshot",
    "build",
    "db_layer",
    "db_layers",
    "drifted",
    "effective",
    "floor_flat",
    "invalidate",
    "read_layer",
    "resolved",
    "yaml_layer",
]
