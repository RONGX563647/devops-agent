"""配置的写入与面板入口 —— 写事务、只存差异、审计、导出。

读路径在 `layers.py`（四层合流），本文件只做三件事：

1. **写入**：`patch` / `reset` 在**一个** `BEGIN IMMEDIATE` 事务里完成「读版本 →
   校验 → 写行 → bump 版本」。旧实现分三次独立连接，并发写同一域时十个请求会拿到
   同一个版本号（乐观锁形同虚设）。
2. **面板**：`index` / `bounds`，一次连接读完所有域，不为每个域各开一次连接。
3. **导出**：把覆盖项导成 yaml 供评审；导出的是「与基线不同的键」，不是当前值。

三条不变量：

- 只存差异：与下层相同的值不落行（等价于恢复默认）；
- `null` 表示删覆盖，不是「值等于 null」；
- 写失败必须吵：坏库拒写（503），而不是写进一个再也读不出来的地方。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import yaml

from devops_agent.config_store import health, schema
from devops_agent.config_store.errors import ConfigConflict as ConfigConflict
from devops_agent.config_store.errors import ConfigUnavailable as ConfigUnavailable
from devops_agent.config_store.errors import InvalidConfig as InvalidConfig
from devops_agent.config_store.errors import UnknownDomain as UnknownDomain
from devops_agent.config_store.layers import TTL_S as TTL_S
from devops_agent.config_store.layers import (
    DbLayer,
    Snapshot,
    build,
    db_layers,
    floor_flat,
    read_layer,
)
from devops_agent.config_store.layers import effective as effective
from devops_agent.config_store.layers import invalidate as invalidate
from devops_agent.config_store.layers import resolved as resolved
from devops_agent.config_store.registry import (
    BY_KEY,
    DomainSpec,
    bounds_of,
    defaults_of,
    env_overlay,
    spec_of,
)
from devops_agent.config_store.structure import MISSING as _MISSING
from devops_agent.config_store.structure import dict_prefixes, maybe_json, unflatten
from devops_agent.config_store.structure import flatten as flatten

logger = logging.getLogger(__name__)

_DB_ERRORS = (OSError, sqlite3.Error)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _payload(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "domain": snapshot.domain,
        "values": snapshot.values,
        "defaults": snapshot.defaults,
        "sources": snapshot.sources,
        "version": snapshot.version,
        "drifted": snapshot.drifted,
    }


def _require_db() -> None:
    if not schema.db_enabled():
        raise InvalidConfig("配置中心在 file 模式下不写入（DEVOPS_AGENT_CONFIG_MODE=file）")


# --- 面板读取 -----------------------------------------------------------


def index() -> list[dict[str, Any]]:
    """面板首页：每个域的标题、生效时机、覆盖条数、被 env 锁定的键。

    一次连接读完全部域 —— 低配机器上「首屏 6 次连接 + 6 次建表」会明显拖慢面板。
    """
    layers = db_layers([spec.key for spec in BY_KEY.values()])
    return [
        {
            "domain": spec.key,
            "title": spec.title,
            "writable": spec.writable,
            "hot": spec.hot,
            "note": spec.note,
            "managed_by": spec.managed_by,
            "version": layers[spec.key].version,
            "overrides": len(layers[spec.key].overrides),
            "env_locked": sorted(env_overlay(spec.key)),
        }
        for spec in BY_KEY.values()
    ]


def bounds(domain: str) -> dict[str, dict[str, Any]]:
    return bounds_of(spec_of(domain))


def audit(domain: str, limit: int = 50) -> list[dict[str, Any]]:
    """最近的改动记录：谁在什么时候把哪个键从什么改成了什么。"""
    spec_of(domain)
    if not schema.db_ready():
        return []
    try:
        with schema.connect() as conn:
            rows = conn.execute(
                """SELECT key, old_value, new_value, version, at, by FROM config_audit
                   WHERE domain=? ORDER BY id DESC LIMIT ?""",
                (domain, max(1, limit)),
            ).fetchall()
    except _DB_ERRORS as exc:
        # 空列表等于「没人改过」，那是谎报；这里宁可明说读不到
        health.note_failure("db", f"audit {type(exc).__name__}: {exc}")
        raise ConfigUnavailable(f"配置库不可读：{type(exc).__name__}: {exc}") from exc
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


def _next_version(conn: sqlite3.Connection, domain: str, at: str) -> int:
    """在事务内 bump 版本号：写锁已在手，读到的就是当前值。"""
    row = conn.execute("SELECT version FROM config_domains WHERE domain=?", (domain,)).fetchone()
    version = (int(row["version"]) if row else 0) + 1
    conn.execute(
        """INSERT INTO config_domains(domain, version, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(domain) DO UPDATE SET
             version=excluded.version, updated_at=excluded.updated_at""",
        (domain, version, at),
    )
    return version


def _write(
    conn: sqlite3.Connection,
    spec: DomainSpec,
    before: DbLayer,
    after: dict[str, Any],
    *,
    by: str,
) -> tuple[int, bool]:
    """在事务内落库 + 审计 + bump 版本；没有任何实际变化时不写、不 bump。"""
    changed = sorted(
        key
        for key in set(before.overrides) | set(after)
        if before.overrides.get(key, _MISSING) != after.get(key, _MISSING)
    )
    if not changed:
        return before.version, False
    defaults = flatten(defaults_of(spec))
    at = _now()
    version = _next_version(conn, spec.key, at)
    for key in changed:
        _write_one(conn, spec.key, key, after, defaults, at, by, version)
    return version, True


def _write_one(
    conn: sqlite3.Connection,
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
                 value=excluded.value,
                 default_at_write=excluded.default_at_write,
                 updated_at=excluded.updated_at,
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


def _transactional(domain: str, work: Any) -> Snapshot:
    """在写事务里执行 `work(conn)`，并把存储故障翻译成 503。"""
    try:
        with schema.transaction() as conn:
            work(conn)
    except _DB_ERRORS as exc:
        health.note_failure("db", f"write {type(exc).__name__}: {exc}")
        raise ConfigUnavailable(f"配置库不可写：{type(exc).__name__}: {exc}") from exc
    invalidate(domain)
    return resolved(domain)


def patch(
    domain: str, incoming: dict[str, Any], *, expected: int | None = None, by: str = "local"
) -> Snapshot:
    """增量修改：`None` 表示删掉该覆盖（回落到下层），不是「设为 None」。"""
    spec = spec_of(domain)
    if not spec.writable:
        raise InvalidConfig(f"{spec.title} 在本接口只读")
    _check_keys(spec, incoming)
    _reject_env_locked(spec, incoming)
    _require_db()

    state: dict[str, Any] = {}

    def work(conn: sqlite3.Connection) -> None:
        layer = read_layer(conn, domain)
        if expected is not None and expected != layer.version:
            raise ConfigConflict(layer.version, _payload(build(spec, layer)))
        candidate = dict(layer.overrides)
        for key, value in incoming.items():
            if value is None:
                candidate.pop(key, None)
            else:
                candidate[key] = value
        normalized = _normalize(floor_flat(spec), candidate)
        build(spec, DbLayer(normalized, layer.at_write, layer.version))  # 校验，越界即抛
        state["persisted"] = _write(conn, spec, layer, normalized, by=by)[1]

    snapshot = _transactional(domain, work)
    return replace(snapshot, persisted=bool(state.get("persisted")))


def reset(
    domain: str, keys: list[str] | None = None, *, all_: bool = False, by: str = "local"
) -> Snapshot:
    """恢复默认：给 keys 只清这几项，all_ 清全部。

    `keys` 也走 `_check_keys` —— 拼错的键静默变成「什么都没做」，比直接报错更难查
    （用户在界面上看到的是「保存成功」）。
    """
    spec = spec_of(domain)
    if not spec.writable:
        raise InvalidConfig(f"{spec.title} 在本接口只读")
    targets = list(keys or ())
    if not all_:
        _check_keys(spec, dict.fromkeys(targets))
    _require_db()

    state: dict[str, Any] = {}

    def work(conn: sqlite3.Connection) -> None:
        layer = read_layer(conn, domain)
        removed = set(layer.overrides) if all_ else set(targets)
        kept = {key: value for key, value in layer.overrides.items() if key not in removed}
        state["persisted"] = _write(conn, spec, layer, kept, by=by)[1]

    snapshot = _transactional(domain, work)
    return replace(snapshot, persisted=bool(state.get("persisted")))


def export_yaml(domain: str) -> str:
    """把该域的覆盖项导出成 yaml：可进 PR 评审，也可当另一台机器的导入源。

    导出的是「与基线不同的键」，不是当前值。`root` 域（policy）的文件顶层就是
    负载本身，所以不再包一层同名段 —— 否则贴回去会多出一层 `policy:`。
    读不到库时直接报 503：导出空文件会被当成「这台机器没有覆盖」。
    """
    spec = spec_of(domain)
    layer = _strict_layer(domain)
    header = (
        f"# {spec.title}：{domain} 域的覆盖项（由配置中心导出，源：config.db）\n"
        "# 只有与基线不同的键才会出现；把它贴回 config/"
        f"{spec.section}.yaml 之前请先评审。\n"
    )
    payload = unflatten(layer.overrides)
    body = yaml.safe_dump(
        payload if spec.root else {spec.section: payload},
        allow_unicode=True,
        sort_keys=True,
    )
    return header + body


def _strict_layer(domain: str) -> DbLayer:
    """导出用：读失败要明说，不能把空覆盖当成事实。"""
    if not schema.db_ready():
        return DbLayer({}, {}, 0)
    try:
        with schema.connect() as conn:
            return read_layer(conn, domain)
    except _DB_ERRORS as exc:
        health.note_failure("db", f"export {type(exc).__name__}: {exc}")
        raise ConfigUnavailable(f"配置库不可读：{type(exc).__name__}: {exc}") from exc


def iter_domains() -> Iterator[DomainSpec]:
    yield from BY_KEY.values()


__all__ = [
    "ConfigConflict",
    "ConfigUnavailable",
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
