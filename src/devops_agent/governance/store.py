"""本地规则沉淀 —— `config/permissions.local.yaml` 的读写。

为什么单独一个文件、且不进 git
------------------------------
「以后都这样」如果只写进 `AgentState.permission_context.allow_rules`，它就随
`session_states` 快照生存：换会话、清历史、会话被 LRU 回收后重建，全部归零。
用户于是每次都在从零教育一个失忆的助手，体感就是「怎么设都没用」。

存到部署级文件里（本后端的所有使用者共享一套，见 ADR-0002 决策四），跨会话、
跨进程、跨重启都还在。文件不进库：它是本机运行痕迹，和 workdir 强相关。

并发与容错
----------
- 写：临时文件 + `os.replace` 原子替换，进程内 `threading.Lock` 串行化。
  用线程锁而不是 `asyncio.Lock`：写入既可能来自 async 端点，也可能来自
  sync 端点（线程池）或启动期装配，线程锁在两种上下文里都成立。
- 读：字段缺失、类型不对、YAML 坏掉一律跳过该条并记 warning —— 沿用
  `available()` 的宽容原则，配置文件写错不该让服务起不来。
- 往返会丢注释：这是机器写的文件，注释写在文档里而不是文件里。
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from devops_agent.config import config_dir
from devops_agent.governance.rules import BEHAVIORS, SCOPES, SOURCES, Rule

logger = logging.getLogger(__name__)

RULES_FILE = "permissions.local.yaml"
# 测试与多部署隔离用：直接指向一个具体文件，优先于 config_dir()
FILE_ENV = "DEVOPS_AGENT_PERMISSIONS_FILE"

_write_lock = threading.Lock()


def rules_path() -> Path:
    """规则文件路径。环境变量优先，便于测试与多实例部署各用一份。"""
    override = os.environ.get(FILE_ENV)
    return Path(override).expanduser() if override else config_dir() / RULES_FILE


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_document(path: Path | None = None) -> dict[str, Any]:
    """整份文件的原始字典；不存在或坏掉返回空字典（永不抛异常）。"""
    target = path if path is not None else rules_path()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("权限规则文件不可读，按空处理: %s (%s)", target, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _to_rule(item: dict[str, Any]) -> Rule | None:
    """一条 YAML 记录 → Rule；字段不合法返回 None（调用方跳过并记 warning）。"""
    tool = str(item.get("tool") or "").strip()
    behavior = str(item.get("behavior") or "ask").strip()
    scope = str(item.get("scope") or "workspace").strip()
    source = str(item.get("source") or "user").strip()
    if not tool or behavior not in BEHAVIORS or scope not in SCOPES or source not in SOURCES:
        logger.warning("跳过非法权限规则: %s", item)
        return None
    pattern = item.get("pattern")
    return Rule(
        tool=tool,
        pattern=None if pattern in (None, "") else str(pattern),
        behavior=behavior,
        source=source,
        scope=scope,
        origin=str(item.get("origin") or ""),
        created_at=str(item.get("created_at") or ""),
    )


def local_rules(path: Path | None = None) -> list[Rule]:
    """文件里沉淀的规则；单条非法只跳过那一条。"""
    raw = read_document(path).get("rules")
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for item in raw:
        if not isinstance(item, dict):
            logger.warning("跳过非法权限规则条目: %r", item)
            continue
        rule = _to_rule(item)
        if rule is not None:
            rules.append(rule)
    return rules


def _dump_rule(rule: Rule) -> dict[str, Any]:
    return {
        "tool": rule.tool,
        "pattern": rule.pattern,
        "behavior": rule.behavior,
        "source": rule.source,
        "scope": rule.scope,
        "origin": rule.origin,
        "created_at": rule.created_at or _now(),
    }


def _write_document(document: dict[str, Any], path: Path | None = None) -> None:
    """原子替换：先写临时文件再 `os.replace`，避免读到半截 YAML。"""
    target = path if path is not None else rules_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        handle, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                yaml.safe_dump(document, fh, allow_unicode=True, sort_keys=False)
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


def add_rule(rule: Rule, path: Path | None = None) -> list[Rule]:
    """沉淀一条规则并落盘；同一 (tool, pattern) 覆盖而不是追加。

    返回落盘后的完整规则集，调用方据此刷新内存态，避免「写了但这次不生效」。
    """
    stored = Rule(
        tool=rule.tool,
        pattern=rule.pattern,
        behavior=rule.behavior,
        source=rule.source if rule.source in SOURCES else "user",
        scope=rule.scope if rule.scope in SCOPES else "workspace",
        origin=rule.origin,
        created_at=rule.created_at or _now(),
    )
    current = local_rules(path)
    merged = [item for item in current if item.identity != stored.identity]
    merged.append(stored)
    document = read_document(path)
    document["rules"] = [_dump_rule(item) for item in merged]
    _write_document(document, path)
    return merged


def remove_rule(tool: str, pattern: str | None, path: Path | None = None) -> bool:
    """按 (tool, pattern) 精确删除；返回是否真的删掉了一条。"""
    current = local_rules(path)
    kept = [item for item in current if item.identity != (tool, pattern)]
    if len(kept) == len(current):
        return False
    document = read_document(path)
    document["rules"] = [_dump_rule(item) for item in kept]
    _write_document(document, path)
    return True


@dataclass(frozen=True, slots=True)
class TrustConfig:
    """信任目录配置。

    `permissive` 不校验（默认，保证既有部署行为不变）；`strict` 下未列出的
    workdir 会被强制降到只读档。ADR 原本写的是「无条件校验」，落地时留了一道
    开关：先让用户的既有部署不被突然锁成只读，再在需要时打开。
    """

    mode: str = "permissive"
    workdirs: tuple[str, ...] = ()

    def is_trusted(self, workdir: str | Path) -> bool:
        if self.mode != "strict":
            return True
        return _realpath(workdir) in self.workdirs


def _realpath(workdir: str | Path) -> str:
    try:
        return str(Path(workdir).resolve())
    except OSError:  # 路径不存在时 resolve 仍会返回绝对路径；这里兜底极端情况
        return str(workdir)


def trust_config(path: Path | None = None) -> TrustConfig:
    raw = read_document(path).get("trust")
    if not isinstance(raw, dict):
        return TrustConfig()
    mode = str(raw.get("mode") or "permissive")
    if mode not in ("permissive", "strict"):
        logger.warning("未知的 trust.mode=%s，按 permissive 处理", mode)
        mode = "permissive"
    entries = raw.get("workdirs")
    workdirs: list[str] = []
    if isinstance(entries, list):
        for item in entries:
            if isinstance(item, str) and item:
                workdirs.append(item)
            elif isinstance(item, dict) and item.get("path"):
                workdirs.append(str(item["path"]))
    return TrustConfig(mode=mode, workdirs=tuple(workdirs))


def trust_workdir(workdir: str | Path, path: Path | None = None) -> TrustConfig:
    """把一个 workdir 加入信任列表（strict 模式下才有效果）。"""
    target = _realpath(workdir)
    current = trust_config(path)
    if target in current.workdirs:
        return current
    document = read_document(path)
    raw = document.get("trust")
    entries = list(raw.get("workdirs") or []) if isinstance(raw, dict) else []
    entries.append({"path": target, "since": _now()})
    document["trust"] = {"mode": current.mode, "workdirs": entries}
    _write_document(document, path)
    return trust_config(path)
