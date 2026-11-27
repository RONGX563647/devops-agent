"""配置的读取与写入 —— 四层合流、只存差异、乐观锁、审计。

读取：代码默认 ← yaml 基线 ← db 覆盖 ← env。每次都从代码默认重建，而不是在旧结果
上打补丁，避免「删掉覆盖后留下的空洞」。
写入：只落与下层不同的键；写前用 pydantic 校验（越界 400），写后 bump 版本（409）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import ValidationError

from devops_agent.config_files import read_yaml
from devops_agent.config_store import schema
from devops_agent.config_store.registry import (
    BY_KEY,
    DomainSpec,
    bounds_of,
    defaults_of,
    env_overlay,
    model_for,
)
from devops_agent.config_store.structure import (
    MISSING as _MISSING,
)
from devops_agent.config_store.structure import (
    dict_prefixes,
    flatten,
    maybe_json,
    merge,
    unflatten,
)

logger = logging.getLogger(__name__)


def _explain(error: ValidationError) -> str:
    """把 pydantic 的第一条错误说成「哪个键、错在哪」，界面据此高亮。"""
    first = error.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "配置"
    return f"{loc}: {first.get('msg', '取值不合法')}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _spec(domain: str) -> DomainSpec:
    spec = BY_KEY.get(domain)
    if spec is None:
        raise UnknownDomain(domain)
    return spec


# 多进程下靠它感知别人写入：一次 TTL 之后重读版本号与覆盖行。
TTL_S = 2.0


class UnknownDomain(KeyError):
    """域不存在：调用方拼错了 key，不能静默按空配置处理。"""


class InvalidConfig(ValueError):
    """值或键不合法：接口层转 400，附字段名让界面高亮那一行。"""


class ConfigConflict(Exception):
    """版本冲突：别人先改过。带当前快照，前端可直接覆盖显示。"""

    def __init__(self, version: int, payload: dict[str, Any]) -> None:
        super().__init__(f"配置版本冲突：当前 v{version}")
        self.version = version
        self.payload = payload


@dataclass(slots=True)
class Snapshot:
    """一次解析的完整结果：生效值、默认值、来源、版本、写入时的默认值。"""

    domain: str
    values: dict[str, Any]
    defaults: dict[str, Any]
    sources: dict[str, str]
    version: int
    drifted: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _DbLayer:
    overrides: dict[str, Any]
    at_write: dict[str, str]
    version: int


_cache: dict[str, tuple[float, Snapshot]] = {}


# --- 各层 ---------------------------------------------------------------


def _yaml_layer(spec: DomainSpec) -> dict[str, Any]:
    """仓库基线：`config/<section>.yaml` 的对应段；缺失即空。"""
    raw = read_yaml(f"{spec.section}.yaml")
    section = raw.get(spec.section, raw)
    return section if isinstance(section, dict) else {}


def _floor_flat(spec: DomainSpec) -> dict[str, Any]:
    """下层合流后的每个键（默认 ← yaml ← env），用来判断「这个值算不算覆盖」。"""
    merged = merge(defaults_of(spec), _yaml_layer(spec))
    merged = merge(merged, unflatten(env_overlay(spec.key)))
    return flatten(merged)


def _db_layer(domain: str) -> _DbLayer:
    """读该域的覆盖行；`file` 模式或库不存在时返回空层。"""
    if not schema.db_ready():
        return _DbLayer({}, {}, 0)
    try:
        with schema.connect() as conn:
            rows = conn.execute(
                "SELECT key, value, default_at_write FROM config_entries WHERE domain=?",
                (domain,),
            ).fetchall()
            row = conn.execute(
                "SELECT version FROM config_domains WHERE domain=?", (domain,)
            ).fetchone()
    except OSError:
        logger.warning("配置库不可读，按无覆盖处理", exc_info=True)
        return _DbLayer({}, {}, 0)
    overrides: dict[str, Any] = {}
    at_write: dict[str, str] = {}
    for item in rows:
        try:
            overrides[str(item["key"])] = json.loads(str(item["value"]))
        except json.JSONDecodeError:
            logger.warning("配置 %s.%s 不是合法 JSON，忽略该行", domain, item["key"])
            continue
        at_write[str(item["key"])] = str(item["default_at_write"] or "")
    return _DbLayer(overrides, at_write, int(row["version"]) if row else 0)


def _build(spec: DomainSpec, layer: _DbLayer) -> Snapshot:
    """把四层合成一份校验过的快照。未知键会被模型丢弃，来源只按生效值统计。"""
    defaults = defaults_of(spec)
    merged = merge(defaults, _yaml_layer(spec))
    merged = merge(merged, unflatten(layer.overrides))
    # env 最高：容器注入的值必须压过库里的值，否则界面显示的和进程跑的不是一回事
    merged = merge(merged, unflatten(env_overlay(spec.key)))
    model = model_for(spec)
    try:
        values = model.model_validate(merged).model_dump()
    except ValidationError as exc:
        raise InvalidConfig(_explain(exc)) from exc
    # 来源只认「这一层自己有没有这个键」：yaml 层用自身键集，不能拿合并后的
    # floor 去判 —— defaults 里的键全在 floor 中，否则 "default" 永远不可达，
    # 界面会把所有代码默认值谎报成 yaml 基线（删掉 yaml 后尤其误导）。
    yaml_keys = flatten(_yaml_layer(spec))
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
    return Snapshot(spec.key, values, defaults, sources, layer.version, _drifted(defaults, layer))


def _drifted(defaults: dict[str, Any], layer: _DbLayer) -> list[str]:
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


# --- 对外读取 -----------------------------------------------------------


def resolved(domain: str) -> Snapshot:
    """带 TTL 缓存的解析结果。"""
    spec = _spec(domain)
    cached = _cache.get(domain)
    now = time.monotonic()
    if cached is not None and now - cached[0] < TTL_S:
        return cached[1]
    snapshot = _build(spec, _db_layer(domain))
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


def index() -> list[dict[str, Any]]:
    """面板首页：每个域的标题、生效时机、覆盖条数、被 env 锁定的键。"""
    out: list[dict[str, Any]] = []
    for spec in BY_KEY.values():
        layer = _db_layer(spec.key)
        out.append(
            {
                "domain": spec.key,
                "title": spec.title,
                "writable": spec.writable,
                "hot": spec.hot,
                "note": spec.note,
                "managed_by": spec.managed_by,
                "version": layer.version,
                "overrides": len(layer.overrides),
                "env_locked": sorted(env_overlay(spec.key)),
            }
        )
    return out


def bounds(domain: str) -> dict[str, dict[str, Any]]:
    return bounds_of(_spec(domain))


def audit(domain: str, limit: int = 50) -> list[dict[str, Any]]:
    """最近的改动记录：谁在什么时候把哪个键从什么改成了什么。"""
    if not schema.db_ready():
        return []
    with schema.connect() as conn:
        rows = conn.execute(
            """SELECT key, old_value, new_value, version, at, by FROM config_audit
               WHERE domain=? ORDER BY id DESC LIMIT ?""",
            (domain, max(1, limit)),
        ).fetchall()
    return [
        {
            "key": str(row["key"]),
            "old": maybe_json(row["old_value"]),
            "new": maybe_json(row["new_value"]),
            "version": int(row["version"]),
            "at": str(row["at"]),
            "by": str(row["by"]),
        }
        for row in rows
    ]


# --- 写入 ---------------------------------------------------------------


def _check_keys(spec: DomainSpec, incoming: dict[str, Any]) -> None:
    """拼错的键必须报错：静默丢弃会让「我明明改了」变成又一个排查不了的怪事。"""
    known = set(flatten(defaults_of(spec)))
    dynamic = dict_prefixes(defaults_of(spec))
    for key in incoming:
        if key in known:
            continue
        if any(key.startswith(f"{prefix}.") for prefix in dynamic):
            continue
        raise InvalidConfig(f"未知配置键 {key}")


def _reject_env_locked(spec: DomainSpec, incoming: dict[str, Any]) -> None:
    """被环境变量锁定的键不收：写进去也赢不过 env，只会造成「保存了却没变」。"""
    locked = set(env_overlay(spec.key))
    hit = sorted(locked & set(incoming))
    if hit:
        raise InvalidConfig(f"{', '.join(hit)} 已被环境变量锁定，请在部署环境里改")


def _normalize(floor: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """只存差异：与下层相同的值不落库（等价于「恢复默认」）。"""
    return {key: value for key, value in candidate.items() if floor.get(key, _MISSING) != value}


def _write(spec: DomainSpec, before: _DbLayer, after: dict[str, Any], *, by: str) -> int:
    """落库 + 审计 + bump 版本；没有任何实际变化时不写、不 bump。"""
    changed = sorted(
        key
        for key in set(before.overrides) | set(after)
        if before.overrides.get(key, _MISSING) != after.get(key, _MISSING)
    )
    if not changed:
        return before.version
    defaults = flatten(defaults_of(spec))
    version = before.version + 1
    at = _now()
    with schema.connect() as conn:
        for key in changed:
            _write_one(conn, spec.key, key, after, defaults, at, by, version)
        conn.execute(
            """INSERT INTO config_domains(domain, version, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET
                 version=excluded.version, updated_at=excluded.updated_at""",
            (spec.key, version, at),
        )
    return version


def _write_one(
    conn: Any,
    domain: str,
    key: str,
    after: dict[str, Any],
    defaults: dict[str, Any],
    at: str,
    by: str,
    version: int,
) -> None:
    """单个键的 upsert / delete + 一条审计。"""
    new = after.get(key, _MISSING)
    old = conn.execute(
        "SELECT value FROM config_entries WHERE domain=? AND key=?", (domain, key)
    ).fetchone()
    if new is _MISSING:
        conn.execute("DELETE FROM config_entries WHERE domain=? AND key=?", (domain, key))
    else:
        recorded = json.dumps(defaults.get(key), ensure_ascii=False)
        conn.execute(
            """INSERT INTO config_entries(domain, key, value, default_at_write,
                                          updated_at, updated_by)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(domain, key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at,
                 updated_by=excluded.updated_by""",
            (domain, key, json.dumps(new, ensure_ascii=False), recorded, at, by),
        )
    conn.execute(
        """INSERT INTO config_audit(domain, key, old_value, new_value, version, at, by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            domain,
            key,
            None if old is None else str(old["value"]),
            None if new is _MISSING else json.dumps(new, ensure_ascii=False),
            version,
            at,
            by,
        ),
    )


def patch(
    domain: str, incoming: dict[str, Any], *, expected: int | None = None, by: str = "local"
) -> Snapshot:
    """增量修改：`None` 表示删掉该覆盖（回落到下层），不是「设为 None」。"""
    spec = _spec(domain)
    if not spec.writable:
        raise InvalidConfig(f"{spec.title} 在本接口只读")
    _check_keys(spec, incoming)
    _reject_env_locked(spec, incoming)
    if not schema.db_enabled():
        raise InvalidConfig("配置中心在 file 模式下不写入（DEVOPS_AGENT_CONFIG_MODE=file）")
    layer = _db_layer(domain)
    if expected is not None and expected != layer.version:
        raise ConfigConflict(layer.version, _payload(_build(spec, layer)))
    candidate = dict(layer.overrides)
    for key, value in incoming.items():
        if value is None:
            candidate.pop(key, None)
        else:
            candidate[key] = value
    floor = _floor_flat(spec)
    normalized = _normalize(floor, candidate)
    _ = _build(spec, _DbLayer(normalized, layer.at_write, layer.version))  # 校验，越界即抛
    _write(spec, layer, normalized, by=by)
    invalidate(domain)
    return resolved(domain)


def reset(
    domain: str, keys: list[str] | None = None, *, all_: bool = False, by: str = "local"
) -> Snapshot:
    """恢复默认：给 keys 只清这几项，all_ 清全部。"""
    spec = _spec(domain)
    if not spec.writable:
        raise InvalidConfig(f"{spec.title} 在本接口只读")
    if not schema.db_enabled():
        raise InvalidConfig("配置中心在 file 模式下不写入（DEVOPS_AGENT_CONFIG_MODE=file）")
    layer = _db_layer(domain)
    targets = set(layer.overrides) if all_ else set(keys or ())
    kept = {key: value for key, value in layer.overrides.items() if key not in targets}
    _write(spec, layer, kept, by=by)
    invalidate(domain)
    return resolved(domain)


def _payload(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "domain": snapshot.domain,
        "values": snapshot.values,
        "defaults": snapshot.defaults,
        "sources": snapshot.sources,
        "version": snapshot.version,
        "drifted": snapshot.drifted,
    }


def export_yaml(domain: str) -> str:
    """把该域的覆盖项导出成 yaml：可进 PR 评审，也可当另一台机器的导入源。"""
    spec = _spec(domain)
    layer = _db_layer(domain)
    header = (
        f"# {spec.title}：{domain} 域的覆盖项（由配置中心导出，源：config.db）\n"
        "# 只有与基线不同的键才会出现；把它贴回 config/"
        f"{spec.section}.yaml 之前请先评审。\n"
    )
    body = yaml.safe_dump(
        {spec.section: unflatten(layer.overrides)},
        allow_unicode=True,
        sort_keys=True,
    )
    return header + body


def iter_domains() -> Iterator[DomainSpec]:
    yield from BY_KEY.values()


__all__ = [
    "ConfigConflict",
    "InvalidConfig",
    "Snapshot",
    "TTL_S",
    "UnknownDomain",
    "audit",
    "bounds",
    "effective",
    "export_yaml",
    "flatten",
    "index",
    "invalidate",
    "iter_domains",
    "patch",
    "reset",
    "resolved",
    "unflatten",
]
