"""多网关模型目录的数据形状 —— 只有类型与常量，不做 IO。

为什么单独一层：`registry`（读 yaml）、`discover`（发 HTTP）、`merge`（四源合并）
都要用同一组形状，把形状塞进任一处都会让另外两处为了一个 dataclass 互相 import。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

#: 无任何来源可查时的兜底窗口。与 `ModelSettings.context_size` 默认值同源，
#: 但刻意不复用那个字段：那是「用户填的」，这是「谁都没说话时的最后一个数」。
DEFAULT_CONTEXT_SIZE = 128_000

#: `discover` 三种模式。字符串而非 Enum：它要直接写进 yaml 与 API。
DISCOVER_MODES: tuple[str, ...] = ("models", "manual", "off")

#: 发现失败的归一码。顺序即严重性（前端按它选颜色）。
CODES: tuple[str, ...] = (
    "ok",
    "not_configured",
    "unauthorized",
    "unsupported",
    "rate_limited",
    "unavailable",
    "empty",
    "malformed",
)


class ProviderError(ValueError):
    """注册表非法。加载期抛：宁可起不来，也不要带着半张表静默运行。"""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True, slots=True)
class KindSpec:
    """一种协议：用哪个模型类、哪个凭据类、怎么发现模型。"""

    model_cls: type
    credential_cls: type
    family: str
    #: 凭据里承载 base_url 的字段名（Gemini 没有，Ollama 叫 host，XAI 叫 api_host）
    url_field: str | None = "base_url"


@dataclass(frozen=True, slots=True)
class ManualModel:
    """`discover: manual` 的人工清单项。`context_size` 可省，省了就走卡/校准。"""

    name: str
    context_size: int | None = None
    label: str = ""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """一个「用户命名的网关实例」。"""

    id: str
    kind: str
    base_url: str
    api_key: str
    label: str = ""
    discover: str = "models"
    models: tuple[ManualModel, ...] = ()

    @property
    def display(self) -> str:
        return self.label or self.id

    @property
    def key_env(self) -> str | None:
        """`env:NAME` 形态时返回变量名；其余返回 None。"""
        return self.api_key[4:] if self.api_key.startswith("env:") else None

    @property
    def resolved_key(self) -> str | None:
        """真正要发的 key；None = 没配（该 provider 标 `not_configured`）。

        `none` 是显式声明「这个网关不需要认证」，与「忘了填 env」必须分开：
        前者可以正常请求，后者要进 `/health` 的 `missing_key`。
        """
        if self.api_key == "none":
            return ""
        if self.api_key.startswith("env:"):
            return os.environ.get(self.api_key[4:]) or None
        return self.api_key or None

    @property
    def has_key(self) -> bool:
        return self.resolved_key is not None

    @property
    def key_fingerprint(self) -> str:
        """sha256 前 8 位：只用于「两次配置是不是同一把 key」。

        不加盐也不加主机名 —— 加盐会让「同一把 key 在两个 provider 上」看不出
        是同一把；8 位哈希对 ≥32 字符的 key 不构成可逆风险。
        """
        key = self.resolved_key
        if not key:
            return ""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]

    @property
    def host(self) -> str:
        """只回主机名：完整 URL 可能带 token 查询参数。"""
        try:
            return urlsplit(self.base_url).hostname or ""
        except ValueError:
            return ""

    @property
    def safe_url(self) -> str:
        """可回显的 base_url：去掉 query 与 fragment。

        编辑页需要知道完整地址（否则改端口要重敲一遍），但 URL 上可能挂着
        `?api_key=...`，所以回显时只保留 scheme/host/path。
        """
        try:
            parts = urlsplit(self.base_url)
        except ValueError:
            return ""
        if not parts.scheme:
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @property
    def configured(self) -> bool:
        """能不能发请求：有地址、且有 key（`none` 也算有）。"""
        return bool(self.base_url) and self.has_key


@dataclass(frozen=True, slots=True)
class DiscoveryItem:
    """归一化后的一个上游模型。窗口/模态可为 None —— 那就是「上游没说」。"""

    id: str
    label: str = ""
    created_at: str = ""
    context_size: int | None = None
    output_size: int | None = None
    input_types: tuple[str, ...] = ()
    output_types: tuple[str, ...] = ()
    status: str = "implicitly_active"
    origin: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label or self.id,
            "created_at": self.created_at or None,
            "context_size": self.context_size,
            "output_size": self.output_size,
            "input_types": list(self.input_types),
            "status": self.status,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """发现结果。`code` 永远有值：调用方按它出文案，不靠猜 None。"""

    items: tuple[DiscoveryItem, ...] = ()
    code: str = "ok"
    fetched_at: str = ""
    stale: bool = False
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.code == "ok"

    def as_dict(self) -> dict[str, object]:
        return {
            "fetched_at": self.fetched_at,
            "stale": self.stale,
            "code": self.code,
            "http_status": self.http_status,
            "total": len(self.items),
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class Capability:
    """一张能力卡：四源合并后的结果 + 每个字段的来源。

    `sources` 与 `parameter_schema` 一起走：只给值不给来源，界面就无法解释
    「为什么这个模型显示 128k」，而那正是这次要修的问题。
    """

    context_size: int
    output_size: int | None = None
    input_types: tuple[str, ...] = ("text/plain",)
    output_types: tuple[str, ...] = ("text/plain",)
    status: str = "implicitly_active"
    label: str = ""
    parameter_schema: dict[str, object] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    matched_by: str = "none"
    matched_card: str | None = None
    deprecated_at: str = ""

    def source_of(self, key: str) -> str:
        return self.sources.get(key, "")

    def as_dict(self) -> dict[str, object]:
        return {
            "context_size": self.context_size,
            "output_size": self.output_size,
            "input_types": list(self.input_types),
            "output_types": list(self.output_types),
            "status": self.status,
            "label": self.label,
            "parameter_schema": self.parameter_schema,
            "deprecated_at": self.deprecated_at or None,
        }


@dataclass(frozen=True, slots=True)
class CatalogSettings:
    """`providers.yaml` 顶层的 `catalog` 段。"""

    enabled: bool = True
    health_check: bool = False
    default: str = ""
