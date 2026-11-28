"""供应商注册表：读 `config/providers.yaml`、校验、合成匿名 `default`。

三条硬规则：

1. **非法配置在加载期报错**，不静默取第一个。重复 id、未知 kind、带凭据的 URL
   都会让「我配了三个网关，实际只跑了一个」这种事故变得可诊断。
2. **没有 `providers.yaml` 时行为与改动前逐字段相同**（SPEC-0018 A35）：
   合成一个匿名 `default`，地址与 key 仍来自那两个既有环境变量。
3. **凭据只存变量名**。注册表可以进 git，密钥留在部署环境。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from devops_agent.config_files import config_dir, read_yaml
from devops_agent.model_catalog.schema import (
    DISCOVER_MODES,
    CatalogSettings,
    ManualModel,
    ProviderError,
    ProviderSpec,
)

logger = logging.getLogger(__name__)

PROVIDERS_FILE = "providers.yaml"
DEFAULT_ID = "default"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: 匿名 provider 的协议由它覆盖。刻意**不做**主机名推断：
#: 今天 DashScope 兼容模式的部署跑的是 `OpenAIChatModel`；若按主机名判成
#: `dashscope_chat`，请求形状、`Parameters` 字段集、`extra_body` 支持全变，
#: A35 的逐字段等价立刻破掉。要换协议请显式声明。
KIND_ENV = "DEVOPS_AGENT_PROVIDER_KIND"
BASE_URL_ENV = "DEVOPS_AGENT_BASE_URL"
API_KEY_ENV = "DEVOPS_AGENT_API_KEY"
MODEL_ENV = "DEVOPS_AGENT_MODEL"

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def is_loopback(host: str) -> bool:
    """回环判定：localhost 与所有 127.0.0.0/8、::1。"""
    if not host:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> None:
    """校验 base_url；非法抛 `ProviderError`，文案直接给改法。

    拒绝的核心理由是「明文发 key」，不是「站点不安全」：内网 http 网关必须可用，
    所以放行回环，其余一律要求 https。
    """
    if not url:
        return
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ProviderError("base_url", f"协议必须是 http/https，收到 {parts.scheme or '空'}")
    if parts.username or parts.password:
        raise ProviderError("base_url", "不允许把凭据写在 URL 里（user:pass@host）")
    if not parts.hostname:
        raise ProviderError("base_url", "缺少主机名")
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise ProviderError(
            "base_url",
            f"明文 http 会把 key 发到 {parts.hostname}；改成 https，或让网关监听 127.0.0.1",
        )


def _require(raw: dict[str, Any], key: str, where: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise ProviderError(f"{where}.{key}", "必填项缺失")
    return raw[key]


def _parse_models(raw: Any, where: str) -> tuple[ManualModel, ...]:
    """人工模型清单：允许写成 `["a", "b"]` 或 `[{name, context_size, label}]`。"""
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        raise ProviderError(f"{where}.models", "应为列表")
    items: list[ManualModel] = []
    for index, item in enumerate(raw):
        spot = f"{where}.models[{index}]"
        if isinstance(item, str):
            if item:
                items.append(ManualModel(name=item))
            continue
        if not isinstance(item, dict):
            raise ProviderError(spot, "应为字符串或映射")
        name = _require(item, "name", spot)
        size = item.get("context_size")
        if size is not None and not isinstance(size, int):
            raise ProviderError(f"{spot}.context_size", "应为整数")
        items.append(
            ManualModel(name=str(name), context_size=size, label=str(item.get("label") or ""))
        )
    return tuple(items)


def _parse_discover(raw: Any, where: str) -> str:
    """`discover` 的三种写法；特别处理 YAML 1.1 的布尔劫持。

    `off` / `on` / `no` / `yes` 在 YAML 1.1 里是**布尔**，`discover: off` 解析出来
    是 `False`。若直接 `str(...) or "models"`，False 是 falsy → 落回 `models`，
    于是「声明离线」变成「每次打开面板都打一次网关」，而且没有任何报错。
    """
    if raw is None or raw == "":
        return "models"
    if isinstance(raw, bool):
        return "models" if raw else "off"
    value = str(raw)
    if value not in DISCOVER_MODES:
        raise ProviderError(f"{where}.discover", f"可选值：{', '.join(DISCOVER_MODES)}")
    return value


def parse_provider(raw: Any, index: int) -> ProviderSpec:
    """一条 provider 的解析与校验。"""
    where = f"providers[{index}]"
    if not isinstance(raw, dict):
        raise ProviderError(where, "应为映射")
    pid = str(_require(raw, "id", where))
    if not ID_PATTERN.match(pid):
        raise ProviderError(
            f"{where}.id", f"{pid!r} 不合法（小写字母/数字/下划线/中划线，≤32 字符）"
        )
    kind = str(_require(raw, "kind", where))
    # 未知 kind 在加载期就吵：等到第一次调用模型才 TypeError，排障成本高一个数量级
    from devops_agent.model_catalog.kinds import KINDS

    if kind not in KINDS:
        raise ProviderError(
            f"{where}.kind", f"未知协议 {kind!r}；合法值：{', '.join(sorted(KINDS))}"
        )
    url = str(raw.get("base_url") or "")
    check_url(url)
    key = raw.get("api_key")
    if key in (None, ""):
        raise ProviderError(f"{where}.api_key", "必填（env:NAME / none / 字面量）")
    key = str(key)
    if not key.startswith("env:") and key != "none":
        # 字面量 key 只该出现在测试夹具里：提交进仓库等于泄露
        logger.warning("providers.%s.api_key 是字面量，建议改为 env:NAME", pid)
    return ProviderSpec(
        id=pid,
        kind=kind,
        base_url=url,
        api_key=key,
        label=str(raw.get("label") or ""),
        discover=_parse_discover(raw.get("discover"), where),
        models=_parse_models(raw.get("models"), where),
    )


@dataclass(frozen=True, slots=True)
class Registry:
    """一份不可变的注册表快照。"""

    providers: tuple[ProviderSpec, ...]
    default_id: str
    settings: CatalogSettings

    @property
    def anonymous(self) -> bool:
        """是否走了「没写 providers.yaml」的兼容路径。

        `context_auto` 的默认值由它决定：匿名 → false（升级不改运行参数），
        有注册表 → true（用户显式进入多网关模式）。
        """
        return len(self.providers) == 1 and self.providers[0].id == DEFAULT_ID

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def ids(self) -> list[str]:
        return [item.id for item in self.providers]

    def find(self, provider_id: str | None) -> ProviderSpec | None:
        if not provider_id:
            return None
        return next((item for item in self.providers if item.id == provider_id), None)

    def get(self, provider_id: str | None = None) -> ProviderSpec:
        """按 id 取；省略取默认。未知 id 抛错并列出可用值。"""
        if not provider_id:
            return self.get(self.default_id)
        found = self.find(provider_id)
        if found is None:
            raise ProviderError(
                "provider", f"未知网关 {provider_id!r}；可用：{', '.join(self.ids()) or '（无）'}"
            )
        return found

    def missing_key(self) -> list[str]:
        """配了地址却没配 key 的 provider：`/health` 用它说清「谁不可用」。"""
        return [item.id for item in self.providers if item.base_url and not item.has_key]

    def has_any_key(self) -> bool:
        return any(item.configured for item in self.providers)


def anonymous_provider() -> ProviderSpec:
    """没有 `providers.yaml` 时的单网关：地址与 key 仍来自既有环境变量。"""
    return ProviderSpec(
        id=DEFAULT_ID,
        kind=os.environ.get(KIND_ENV) or "openai_chat",
        label="默认网关",
        base_url=os.environ.get(BASE_URL_ENV, ""),
        api_key=f"env:{API_KEY_ENV}",
        discover="off",
    )


def _parse_all(entries: list[Any]) -> tuple[ProviderSpec, ...]:
    """逐条解析并查重。重复 id 会让「我配了三个网关」变成「只跑了一个」。"""
    specs: list[ProviderSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        spec = parse_provider(item, index)
        if spec.id in seen:
            raise ProviderError(f"providers[{index}].id", f"重复的 id {spec.id!r}")
        seen.add(spec.id)
        specs.append(spec)
    return tuple(specs)


def _settings(catalog_raw: dict[str, Any], default: str) -> CatalogSettings:
    return CatalogSettings(
        enabled=bool(catalog_raw.get("enabled", True)),
        health_check=bool(catalog_raw.get("health_check", False)),
        default=default,
    )


def _build(raw: dict[str, Any]) -> Registry:
    """把 yaml 顶层映射变成注册表；`providers: []` 与文件缺失等价。"""
    entries = raw.get("providers") or []
    if not isinstance(entries, list):
        raise ProviderError("providers", "应为列表")
    if not entries:
        return Registry((anonymous_provider(),), DEFAULT_ID, CatalogSettings())

    specs = _parse_all(entries)
    catalog_raw = raw.get("catalog") or {}
    if not isinstance(catalog_raw, dict):
        raise ProviderError("catalog", "应为映射")
    default = str(raw.get("default") or catalog_raw.get("default") or specs[0].id)
    known = {spec.id for spec in specs}
    if default not in known:
        raise ProviderError(
            "default", f"{default!r} 不在 providers 里；可用：{', '.join(sorted(known))}"
        )
    return Registry(specs, default, _settings(catalog_raw, default))


@lru_cache(maxsize=8)
def _load_cached(directory: str, mtime: float, base_url: str, kind: str) -> Registry:
    """`directory/mtime` 管文件变化，`base_url/kind` 管匿名 provider 的 env 变化。

    后两项必须进缓存键：匿名 provider 的地址直接来自 `DEVOPS_AGENT_BASE_URL`，
    只按文件 mtime 缓存会让「进程启动时 env 还没设、之后设了」的场景永远读到空地址
    （测试里每个用例都 patch env，这条漏了就是整片失败）。
    """
    _ = directory, mtime, base_url, kind
    return _build(read_yaml(PROVIDERS_FILE))


def load_registry() -> Registry:
    """读注册表。缓存键含目录、mtime 与匿名 provider 的两个 env。"""
    path: Path = config_dir() / PROVIDERS_FILE
    mtime = path.stat().st_mtime if path.is_file() else 0.0
    try:
        return _load_cached(
            str(config_dir()),
            mtime,
            os.environ.get(BASE_URL_ENV, ""),
            os.environ.get(KIND_ENV, ""),
        )
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - yaml 结构错误转成可读的配置错误
        raise ProviderError(PROVIDERS_FILE, f"加载失败：{type(exc).__name__}: {exc}") from exc


def invalidate() -> None:
    """测试与热加载用：清掉注册表缓存。"""
    _load_cached.cache_clear()
