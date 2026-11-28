"""`config/providers.yaml` 的带校验写入口（SPEC-0022 §6）。

为什么写文件而不是写 `config.db`：这里存的是**凭据引用**（`env:NAME`），
这份文件要能进 code review、要在没有数据库的环境（CI、`MODE=file`）独立工作，
并且被 `model_catalog` 在装配期直接读。塞进 SQLite 会让「模型从哪来」
依赖一个运行态二进制文件 —— 与 ADR-0005 对 `gates.yaml` 的处置同一理由。

三条刻意的约束：

1. **校验全部在写盘前**，并且用 `parse_provider` 再跑一遍：注册表解析是唯一裁判，
   避免「接口校验过、加载器炸」。
2. **明文 key 硬拦**。只收环境变量名或 `none`，不做「警告后放行」——
   凭据一旦进 git，撤不回来。
3. **写盘用临时文件 + `os.replace`**：中途失败不动原文件，读者永远看到完整版本。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from devops_agent.config_files import config_dir
from devops_agent.model_catalog.registry import (
    ID_PATTERN,
    PROVIDERS_FILE,
    check_url,
    parse_provider,
)
from devops_agent.model_catalog.schema import ProviderError

#: 固定头部：`safe_dump` 不保留手写注释，所以每次写盘都重建这一段。
HEADER = """\
# 多网关注册表（SPEC-0018 / SPEC-0022）。
# 由设置页「模型与网关 › 网关」写入，也可以手改 —— 保存后按模板重排，手写注释会丢。
# api_key 只写环境变量名（env:NAME）或 none：本文件要能进 git。
"""

#: 环境变量名：与 shell 惯例一致，否则 `os.environ.get` 取不到。
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: 明文 key 的形状：前缀型与「长且无下划线」型。命中即 400，不留后门。
PLAINTEXT_PREFIX = re.compile(r"^(sk-|ghp_|gho_|AKIA|xox[baprs]-)")
MAX_KEY_CHARS = 64


class AdminError(Exception):
    """写入口的失败。`code` 直接进响应体，前端按它出文案。"""

    def __init__(self, code: str, reason: str, status: int = 400, **extra: Any) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.status = status
        self.extra = extra


def path_of() -> Path:
    return config_dir() / PROVIDERS_FILE


def read_raw() -> dict[str, Any]:
    """读整份文件。语法错误直接报错，绝不覆盖一份读不懂的文件。"""
    path = path_of()
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise AdminError("providers_unreadable", f"yaml 解析失败：{exc}", 500) from exc
    if not isinstance(raw, dict):
        raise AdminError("providers_unreadable", "顶层必须是映射", 500)
    return raw


def entries_of(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = raw.get("providers") or []
    if not isinstance(items, list):
        raise AdminError("providers_invalid", "providers 应为列表", 500)
    return [item for item in items if isinstance(item, dict)]


def _canonical(raw: dict[str, Any]) -> str:
    """版本的摘要口径：只看内容，不看注释与排版。

    用「注释也算变化」会让一次无关的手改把界面上的保存全部打成 409；
    反过来漏掉 `catalog` 段又会让「改了开关却检测不到冲突」。
    """
    payload = {
        "providers": raw.get("providers") or [],
        "default": raw.get("default") or "",
        "catalog": raw.get("catalog") or {},
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def version_of(raw: dict[str, Any]) -> str:
    """乐观锁凭证：内容摘要前 12 位。前端原样带回。"""
    return hashlib.sha256(_canonical(raw).encode("utf-8")).hexdigest()[:12]


def summarize(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """脱敏清单：只回变量名与 host，不回 key（这一屏常被截屏）。"""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(entries_of(raw)):
        spec = parse_provider(item, index)
        out.append(
            {
                "id": spec.id,
                "kind": spec.kind,
                "label": spec.display,
                "base_url_host": spec.host,
                "api_key_ref": spec.key_env or ("none" if spec.api_key == "none" else ""),
                "discover": spec.discover,
            }
        )
    return out


def _key_ref(value: str) -> str:
    """把界面输入的「变量名」翻译成 `env:NAME`，同时挡掉明文 key。"""
    text = (value or "").strip()
    if not text:
        raise AdminError("invalid_provider", "api_key_ref 必填（环境变量名或 none）")
    if text == "none":
        return "none"
    name = text[4:] if text.startswith("env:") else text
    if not name:
        raise AdminError("invalid_provider", "env: 后面缺少变量名")
    if PLAINTEXT_PREFIX.match(name) or (len(name) > MAX_KEY_CHARS and "_" not in name):
        raise AdminError(
            "plaintext_key",
            "这里填环境变量名，不要粘贴密钥；密钥请写进部署环境",
        )
    if not ENV_NAME.match(name):
        raise AdminError(
            "invalid_provider",
            f"{name!r} 不像环境变量名（字母/数字/下划线，且不以数字开头）",
        )
    return f"env:{name}"


def _clean_id(provider_id: str | None, body: dict[str, Any]) -> str:
    """id 只在这里判一次：新建取请求体，编辑时以路径参数为准（id 不可改）。"""
    pid = str(provider_id or body.get("id") or "")
    if not ID_PATTERN.match(pid):
        raise AdminError(
            "invalid_provider",
            f"{pid!r} 不合法（小写字母/数字/下划线/中划线，2–32 字符）",
        )
    return pid


def _clean(body: dict[str, Any], *, provider_id: str | None = None) -> dict[str, Any]:
    """一条 provider 的白名单化 + 校验。`provider_id` 给了表示这是改，id 不可变。"""
    if not isinstance(body, dict):
        raise AdminError("invalid_provider", "provider 应为映射")
    item = {
        "id": _clean_id(provider_id, body),
        "kind": str(body.get("kind") or ""),
        "label": str(body.get("label") or ""),
        "base_url": _clean_url(str(body.get("base_url") or "")),
        "api_key": _key_ref(str(body.get("api_key_ref") or body.get("api_key") or "")),
        "discover": _discover(body.get("discover")),
    }
    # 白名单之外的键直接丢弃：界面不该成为「往 yaml 里塞任意结构」的通道
    try:
        parse_provider(item, 0)
    except ProviderError as exc:
        raise AdminError(exc.field, exc.reason) from exc
    return item


def _clean_url(base_url: str) -> str:
    """地址只允许 http(s) 且明文 http 只给本机 —— 判定统一交给注册表。"""
    try:
        check_url(base_url)
    except ProviderError as exc:
        raise AdminError(exc.field, exc.reason) from exc
    return base_url


def _discover(value: Any) -> str:
    """`discover` 收 False 是历史写法（等价 off），其余原样交给 `parse_provider`。"""
    return "off" if value is False else str(value or "models")


def _check_default(raw: dict[str, Any], wanted: str | None) -> str:
    ids = [str(item.get("id") or "") for item in entries_of(raw)]
    if not ids:
        return ""
    current = str(raw.get("default") or ids[0])
    value = current if wanted is None else str(wanted)
    if value and value not in ids:
        raise AdminError("invalid_default", f"默认网关 {value!r} 不在 providers 里")
    return value


def _write(raw: dict[str, Any]) -> str:
    """写盘并清缓存。返回新的内容版本。"""
    if not raw.get("providers"):
        raw.pop("default", None)
    text = HEADER + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    path = path_of()
    temp = path.with_suffix(".yaml.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise AdminError("not_persisted", f"无法写入 {path}：{exc}", 503) from exc
    _refresh_catalog()
    return version_of(raw)


def _refresh_catalog() -> None:
    """改完文件让本进程立刻读到：注册表按 mtime 缓存，但显式失效更可靠。"""
    from devops_agent.model_catalog import reset_catalog

    reset_catalog()


def _guard_version(raw: dict[str, Any], expected: str | None) -> None:
    if not expected:
        raise AdminError("version_required", "改动网关必须带 expected_version", 400)
    current = version_of(raw)
    if expected != current:
        raise AdminError(
            "providers_version_conflict",
            "文件已被别人改过，请刷新后重试",
            409,
            version=current,
            providers=summarize(raw),
        )


def create(body: dict[str, Any], *, expected: str | None, default: str | None) -> str:
    raw = read_raw()
    _guard_version(raw, expected)
    item = _clean(body)
    items = entries_of(raw)
    if any(str(entry.get("id") or "") == item["id"] for entry in items):
        raise AdminError("duplicate_id", f"网关 {item['id']!r} 已存在", 409)
    raw["providers"] = [*items, item]
    raw["default"] = _check_default(raw, default or item["id"])
    return _write(raw)


def update(
    provider_id: str, body: dict[str, Any], *, expected: str | None, default: str | None
) -> str:
    raw = read_raw()
    _guard_version(raw, expected)
    items = entries_of(raw)
    index = next(
        (pos for pos, entry in enumerate(items) if str(entry.get("id") or "") == provider_id),
        None,
    )
    if index is None:
        raise AdminError("unknown_provider", f"未知网关 {provider_id!r}", 404)
    items[index] = _clean(body, provider_id=provider_id)
    raw["providers"] = items
    raw["default"] = _check_default(raw, default)
    return _write(raw)


def _reassign(
    raw: dict[str, Any],
    provider_id: str,
    remaining: list[dict[str, Any]],
    reassign_to: str | None,
) -> None:
    """删掉默认网关时必须当场交出接任者，否则会话会指向一个不存在的家。"""
    was_default = str(raw.get("default") or "") == provider_id or not raw.get("default")
    if not (was_default and remaining):
        return
    ids = [str(item.get("id") or "") for item in remaining]
    if not reassign_to:
        raise AdminError(
            "reassign_required",
            "删的是默认网关，必须指定 reassign_to",
            400,
            available=ids,
        )
    if reassign_to not in ids:
        raise AdminError("invalid_default", f"{reassign_to!r} 不在剩余网关里")
    raw["default"] = reassign_to


def delete(provider_id: str, *, expected: str | None, reassign_to: str | None) -> str:
    raw = read_raw()
    _guard_version(raw, expected)
    items = entries_of(raw)
    remaining = [entry for entry in items if str(entry.get("id") or "") != provider_id]
    if len(remaining) == len(items):
        raise AdminError("unknown_provider", f"未知网关 {provider_id!r}", 404)
    raw["providers"] = remaining
    _reassign(raw, provider_id, remaining, reassign_to)
    return _write(raw)


__all__ = [
    "AdminError",
    "HEADER",
    "create",
    "delete",
    "entries_of",
    "path_of",
    "read_raw",
    "summarize",
    "update",
    "version_of",
]
