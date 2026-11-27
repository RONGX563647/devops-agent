"""协议种类 → 框架类 的显式映射。

刻意写成一张表而不是按名字反射（对照 SPEC-0017 §1.5：靠 `getattr` 拼类名
会在框架改名时静默取到错误对象）。表里同时写明「凭据里哪个字段装 base_url」，
因为四家不一样：OpenAI 族叫 `base_url`、Ollama 叫 `host`、XAI 叫 `api_host`、
Gemini 压根没有（只能走 `client_kwargs.http_options`）。
"""

from __future__ import annotations

from functools import cache
from typing import Any

from agentscope.credential import (
    AnthropicCredential,
    DashScopeCredential,
    DeepSeekCredential,
    GeminiCredential,
    MiniMaxCredential,
    MoonshotCredential,
    OllamaCredential,
    OpenAICredential,
    VolcengineCredential,
    XAICredential,
)
from agentscope.model import (
    AnthropicChatModel,
    DashScopeChatModel,
    DeepSeekChatModel,
    GeminiChatModel,
    MiniMaxChatModel,
    MoonshotChatModel,
    OllamaChatModel,
    OpenAIChatModel,
    OpenAIResponseModel,
    VolcengineChatModel,
    XAIChatModel,
)
from pydantic import SecretStr

from devops_agent.model_catalog.schema import KindSpec, ProviderError, ProviderSpec

KINDS: dict[str, KindSpec] = {
    "openai_chat": KindSpec(OpenAIChatModel, OpenAICredential, "openai"),
    "openai_response": KindSpec(OpenAIResponseModel, OpenAICredential, "openai"),
    "dashscope_chat": KindSpec(DashScopeChatModel, DashScopeCredential, "dashscope"),
    "anthropic_chat": KindSpec(AnthropicChatModel, AnthropicCredential, "anthropic"),
    # Gemini 的凭据类没有 base_url：地址只能塞进 client_kwargs
    "gemini_chat": KindSpec(GeminiChatModel, GeminiCredential, "gemini", url_field=None),
    "deepseek_chat": KindSpec(DeepSeekChatModel, DeepSeekCredential, "openai"),
    "moonshot_chat": KindSpec(MoonshotChatModel, MoonshotCredential, "openai"),
    "xai_chat": KindSpec(XAIChatModel, XAICredential, "openai", url_field="api_host"),
    "minimax_chat": KindSpec(MiniMaxChatModel, MiniMaxCredential, "openai"),
    "volcengine_chat": KindSpec(VolcengineChatModel, VolcengineCredential, "openai"),
    "ollama_chat": KindSpec(OllamaChatModel, OllamaCredential, "ollama", url_field="host"),
}

#: 接受 `extra_body` 的 kind。实测只有 `OpenAIChatModel.__init__` 有这个参数：
#: `DashScopeChatModel` 没有，所以联网检索字段（SPEC-0011）只能随 openai 族下发。
#: spec §7.3 写的是「openai / dashscope 族」，此处按真实签名收窄。
EXTRA_BODY_KINDS: frozenset[str] = frozenset({"openai_chat"})

#: 需要 base_url 但没有凭据字段可放的 kind：地址走 client_kwargs。
CLIENT_KWARGS_KINDS: frozenset[str] = frozenset({"gemini_chat"})


def kind_of(spec: ProviderSpec) -> KindSpec:
    """取 kind 描述；未知 kind 是配置错误，不能静默回落到 openai。"""
    try:
        return KINDS[spec.kind]
    except KeyError:
        raise ProviderError(
            f"providers.{spec.id}.kind",
            f"未知协议 {spec.kind!r}；合法值：{', '.join(sorted(KINDS))}",
        ) from None


def build_credential(spec: ProviderSpec) -> Any:
    """按 kind 组装凭据对象。缺 key 的 provider 不该走到这里（调用方先判 configured）。"""
    key = spec.resolved_key
    if key is None:
        raise ProviderError(f"providers.{spec.id}.api_key", "凭据未配置，无法构造模型")
    kind = kind_of(spec)
    fields: dict[str, Any] = {"api_key": SecretStr(key)}
    if kind.url_field is not None and spec.base_url:
        fields[kind.url_field] = spec.base_url
    # Ollama 没有 api_key 字段：无认证网关只认 host
    if spec.kind == "ollama_chat":
        fields = {"host": spec.base_url}
    return kind.credential_cls(**fields)


def build_parameters(kind: str, sampling: dict[str, Any]) -> Any:
    """把采样参数塞进目标模型的 `Parameters`；`None` 与「该类没有这个字段」都丢掉。

    各家的 `Parameters` 字段集不同（Anthropic 没有 temperature/top_p，
    DashScope 没有 reasoning_effort，OpenAIResponse 没有 top_p），传全集必 TypeError。
    所以按目标类的真实字段过滤 —— 这不是宽容，而是「参数只在支持的模型上生效」。
    """
    spec = KINDS.get(kind)
    if spec is None:
        raise ProviderError("kind", f"未知协议 {kind!r}")
    fields = getattr(spec.model_cls, "Parameters", None)
    allowed = set(fields.model_fields) if fields is not None else set()
    payload = {
        name: value for name, value in sampling.items() if value is not None and name in allowed
    }
    return fields(**payload) if fields is not None else None


def build_client_kwargs(spec: ProviderSpec) -> dict[str, Any] | None:
    """Gemini 这类「凭据里放不下地址」的 kind，把 base_url 转成 client 选项。"""
    if spec.kind not in CLIENT_KWARGS_KINDS or not spec.base_url:
        return None
    return {"http_options": {"base_url": spec.base_url}}


@cache
def kinds_payload() -> list[dict[str, str]]:
    """`GET /api/providers` 的可用协议清单：前端下拉要比自由文本可靠。"""
    return [{"kind": name, "family": spec.family} for name, spec in sorted(KINDS.items())]
