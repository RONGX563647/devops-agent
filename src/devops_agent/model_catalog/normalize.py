"""上游 `/models` 响应归一化 —— 形状差异只收在这一层。

为什么必须归一：`id` 是唯一所有网关都保证的字段。窗口字段五家五名
（vLLM `max_model_len`、OpenRouter `context_length`、Groq `context_window`、
Mistral `max_context_length`、Gemini `inputTokenLimit`），模态只有 OpenRouter 给。
不归一就得在合并层写五个分支，而「新增一个网关」会变成「改五个地方」。
"""

from __future__ import annotations

from typing import Any

from devops_agent.model_catalog.schema import DiscoveryItem

#: 窗口字段同义表：**新增网关时只改这里**。顺序即优先级（同一响应里可能多个都在）。
WINDOW_KEYS: tuple[str, ...] = (
    "max_model_len",
    "context_length",
    "context_window",
    "max_context_length",
    "inputTokenLimit",
)

#: 输出上限同义表。
OUTPUT_KEYS: tuple[str, ...] = ("max_output_tokens", "outputTokenLimit", "max_completion_tokens")

#: OpenRouter 的模态名 → MIME 粗分类。其它网关不声明模态，回落到纯文本。
_MODALITIES = {
    "text": "text/plain",
    "image": "image/*",
    "file": "application/pdf",
    "audio": "audio/*",
    "video": "video/*",
}


def _int(value: Any) -> int | None:
    """正整数解析。bool 要单独挡（`isinstance(True, int)` 为真），0 当没给。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _first_int(raw: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        got = _int(raw.get(key))
        if got is not None:
            return got
    return None


def _window(raw: dict[str, Any]) -> int | None:
    """窗口：先看平铺字段，再看 OpenRouter 的 `top_provider.context_length`。"""
    got = _first_int(raw, WINDOW_KEYS)
    if got is not None:
        return got
    top = raw.get("top_provider")
    if isinstance(top, dict):
        return _int(top.get("context_length"))
    return None


def _modalities(raw: dict[str, Any]) -> tuple[str, ...] | None:
    """OpenRouter 的 `architecture.input_modalities`；没声明返回 None（不是空元组）。

    None 与 () 语义不同：前者是「上游没说」，后者是「明确说没有模态」——
    合并层要能区分，否则会把「没说」当成「不支持任何输入」。
    """
    architecture = raw.get("architecture")
    if not isinstance(architecture, dict):
        return None
    declared = architecture.get("input_modalities")
    if not isinstance(declared, list):
        return None
    mapped = tuple(dict.fromkeys(_MODALITIES.get(str(item), f"{item}/*") for item in declared))
    return mapped or None


def _created(raw: dict[str, Any]) -> str:
    """创建时间一律转字符串：各家用 int 时间戳或 ISO 串，统一成「可展示」。"""
    for key in ("created_at", "created", "modified_at"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def _item(raw: dict[str, Any], model_id: str, origin: str) -> DiscoveryItem:
    label = str(raw.get("display_name") or raw.get("displayName") or raw.get("name") or model_id)
    modalities = _modalities(raw)
    return DiscoveryItem(
        id=model_id,
        label=label,
        created_at=_created(raw),
        context_size=_window(raw),
        output_size=_first_int(raw, OUTPUT_KEYS),
        input_types=modalities or (),
        output_types=("text/plain",),
        status="implicitly_active",
        origin=origin,
    )


def _openai_like(payload: dict[str, Any], origin: str) -> list[DiscoveryItem] | None:
    """OpenAI / Anthropic / vLLM / OpenRouter 等：`data[]`，id 在 `id`。"""
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    items: list[DiscoveryItem] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        model_id = raw.get("id") or raw.get("name")
        if model_id:
            items.append(_item(raw, str(model_id), origin))
    return items


def _gemini(payload: dict[str, Any]) -> list[DiscoveryItem] | None:
    """Gemini：`models[]`，id 是 `models/<name>`，且要按 supportedGenerationMethods 过滤。"""
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    items: list[DiscoveryItem] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        if not name:
            continue
        methods = raw.get("supportedGenerationMethods")
        # 明确给了方法列表却不是 chat 模型（如纯 embedding）：列出来也选不了
        if isinstance(methods, list) and methods and "generateContent" not in methods:
            continue
        # `models/gemini-2.5-pro` → `gemini-2.5-pro`：前缀是该 API 的命名空间，不是模型名
        items.append(_item(raw, name.split("/", 1)[-1], "gemini"))
    return items


def _ollama(payload: dict[str, Any]) -> list[DiscoveryItem] | None:
    """Ollama：`models[]`，id 在 `name`；窗口要 `/api/show`，本期取不到（留 None）。"""
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    items: list[DiscoveryItem] = []
    for raw in models:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name") or raw.get("model")
        if name:
            items.append(_item(raw, str(name), "ollama"))
    return items


def normalize(payload: Any, family: str) -> list[DiscoveryItem] | None:
    """按协议族归一化。返回 None = 形状不认识（调用方记 `malformed`）。

    刻意不抛异常也不猜：一个把 `{"error": "..."}` 当 200 返回的网关，
    如果被当成空列表，面板会显示「没有任何模型」—— 那比明确报 malformed 更难查。
    """
    if not isinstance(payload, dict):
        return None
    if family == "gemini":
        return _gemini(payload)
    if family == "ollama":
        return _ollama(payload)
    return _openai_like(payload, family)
