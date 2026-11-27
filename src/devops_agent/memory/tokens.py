"""Token 计数 —— 全仓唯一的估算入口（SPEC-0016 §4）。

三层精度，优先级 L1 > L2，L0 只用于校准：

- L0：供应商返回的 `usage.prompt_tokens`，真值，不参与执行；
- L1：可选真实编码器（`[tokenize]` extra → `tiktoken`），装了才用；
- L2：分族系数估算，恒可用（本模块的最小实现）。

分类计数的理由：单一「字符数 / 常数」在两个方向都不准 —— 中文在 cl100k 上约
0.76 字/token，英文散文约 5.9 字符/token，而紧凑 JSON 只有 2.4 字符/token。
把 ASCII 再分成「字母」与「符号/数字」两类，是让 JSON 与日志不把估算拖低
（低估会顶到窗口边界）的最低成本做法。系数按族给出，锚点是真实样本：
`tests/fixtures/token_calibration.json` 里的 `shapes[]` 由 tiktoken 实测，
`expected_tokens=3060` 是线上一次 `usage.prompt_tokens` 的真值。

计数口径只允许两处：本模块（估算）与框架侧 `count_context_tokens()`（真值）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

#: 每条消息的角色、分隔符与包装开销（主流 chat 模板 3~4 token/条）
PER_MESSAGE_OVERHEAD = 4

#: 族 → (cjk 字/token, 字母/token, 符号数字/token)。
#: cl100k / o200k 由 tiktoken 实测拟合（误差见 fixture）；qwen 以线上真值
#: 3060 为锚整体收紧 6%（token 更多 = 更保守）；generic 取最保守的一组，
#: 未知族宁可多算，也不要因为低估把请求顶到窗口边界。
FAMILIES: dict[str, tuple[float, float, float]] = {
    "cl100k": (0.76, 5.90, 2.36),
    "o200k": (1.22, 5.72, 2.38),
    "qwen": (0.72, 5.57, 2.23),
    "generic": (0.72, 5.57, 2.23),
}


def _classes(text: str) -> tuple[int, int, int]:
    """切成 (汉字, 拉丁字母, 其余) 三类计数，供系数加权。"""
    cjk = 0
    letters = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            letters += 1
    return cjk, letters, len(text) - cjk - letters


@dataclass(frozen=True, slots=True)
class Tokenizer:
    """一次计数的全部参数：三族系数 + 消息开销 + 可选真编码器。"""

    family: str = "generic"
    cjk_per_token: float = FAMILIES["generic"][0]
    letter_per_token: float = FAMILIES["generic"][1]
    symbol_per_token: float = FAMILIES["generic"][2]
    per_message_overhead: int = PER_MESSAGE_OVERHEAD
    #: 真编码器（有 `encode` 即 L1）；None 走 L2 系数
    encoder: Any = field(default=None, repr=False, compare=False)

    @property
    def precise(self) -> bool:
        return self.encoder is not None

    def count(self, text: str) -> int:
        """单段文本的 token 数；空串为 0，向上取整（宁多不少）。"""
        if not text:
            return 0
        if self.encoder is not None:
            return len(self.encoder.encode(text))
        cjk, letters, symbols = _classes(text)
        return int(
            math.ceil(
                cjk / self.cjk_per_token
                + letters / self.letter_per_token
                + symbols / self.symbol_per_token
            )
        )

    def count_messages(self, messages: list[Any]) -> int:
        """消息序列：逐条正文 + 每条固定开销。

        工具结果块的真实字段是 `output`（不是 `text`）；旧实现按 `text` 取值，
        等于把整段工具输出算成 0 —— 输出越长，低估越多。
        """
        return sum(self.count(message_text(msg)) for msg in messages) + (
            self.per_message_overhead * len(messages)
        )


def message_text(msg: Any) -> str:
    """把一条消息压成「会被模型读到」的文本：正文块、工具结果、工具入参。"""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, str):
            parts.append(block)
            continue
        for attr in ("text", "output", "input"):
            value = getattr(block, attr, None)
            if isinstance(value, str):
                parts.append(value)
                break
            if isinstance(value, (dict, list)):
                parts.append(json.dumps(value, ensure_ascii=False, default=str))
                break
    return "\n".join(parts)


def build(
    *,
    family: str = "generic",
    cjk_per_token: float | None = None,
    letter_per_token: float | None = None,
    symbol_per_token: float | None = None,
    per_message_overhead: int | None = None,
    encoder: Any = None,
) -> Tokenizer:
    """按族系数构造；显式传入的系数覆盖族默认值（配置里可以逐项微调）。"""
    default_cjk, default_letter, default_symbol = FAMILIES.get(family, FAMILIES["generic"])
    return Tokenizer(
        family=family,
        cjk_per_token=float(cjk_per_token or default_cjk),
        letter_per_token=float(letter_per_token or default_letter),
        symbol_per_token=float(symbol_per_token or default_symbol),
        per_message_overhead=(
            PER_MESSAGE_OVERHEAD if per_message_overhead is None else int(per_message_overhead)
        ),
        encoder=encoder,
    )


def _load_encoder(family: str) -> Any:
    """尝试 L1 真编码器；没装 `tiktoken` 就返回 None，不影响任何调用方。"""
    try:  # pragma: no cover - 依赖可选包，CI 不装
        import tiktoken
    except ImportError:
        return None
    name = {"cl100k": "cl100k_base", "o200k": "o200k_base"}.get(family)
    if name is None:
        return None
    try:  # pragma: no cover - 同上
        return tiktoken.get_encoding(name)
    except Exception:  # noqa: BLE001 - 编码器缺失只降级，不抛给主流程
        return None


@lru_cache(maxsize=1)
def default() -> Tokenizer:
    """部署默认计数器：读 `model.yaml` 的 tokenizer 段，缺字段回落 generic。

    延迟 import 避免 `config` 与本模块成环；`precision: exact` 且装了
    `tiktoken` 时才升级到 L1，否则恒走 L2 —— 默认行为必须与是否装可选包无关。
    """
    from devops_agent.config import load_model

    settings = getattr(load_model(), "tokenizer", None)
    family = str(getattr(settings, "family", "generic") or "generic")
    encoder = None
    if str(getattr(settings, "precision", "estimate")) == "exact":
        encoder = _load_encoder(family)
    return build(
        family=family,
        cjk_per_token=getattr(settings, "cjk_per_token", None),
        letter_per_token=getattr(settings, "letter_per_token", None),
        symbol_per_token=getattr(settings, "symbol_per_token", None),
        per_message_overhead=getattr(settings, "per_message_overhead", None),
        encoder=encoder,
    )


def estimate_tokens(text: str) -> int:
    """单段文本估算（纯函数口径，不叠加漂移修正）。"""
    return default().count(text)


def count_messages(messages: list[Any], *, apply_drift: bool = True) -> int:
    """消息序列估算；`apply_drift` 时按滚动中位数比率修正（SPEC-0016 §4.3）。"""
    from devops_agent.memory.calibration import alpha

    raw = default().count_messages(messages)
    return int(math.ceil(raw * alpha())) if apply_drift else raw


def count_prefix(system_text: str, tools: list[dict[str, Any]], *, apply_drift: bool = True) -> int:
    """固定前缀（系统提示 + 工具 schema）；工具 schema 走同一套系数。"""
    from devops_agent.memory.calibration import alpha

    raw = default().count(system_text) + default().count(json.dumps(tools, ensure_ascii=False))
    return int(math.ceil(raw * alpha())) if apply_drift else raw


# --- 兼容入口 -------------------------------------------------------------
# `count_messages` 是带系数的正式口径；下面三个名字保留给只想要「一段文本 / 一条
# 消息 / 整段历史」的调用方（裁剪与观测都在用）。同一份文本两次取数不能有两套
# 实现：这里一律回落 `default()`，与预算、压缩读的是同一个计数器。


def block_text(block: Any) -> str:
    """任意内容块 → 文本：工具结果取 `output`（字段名不是 `text`），文本块取 `text`。"""
    for attr in ("text", "output", "input"):
        value = getattr(block, attr, None)
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
    return ""


def content_text(content: Any) -> str:
    """消息内容 → 文本：字符串原样，块列表逐块取文本后拼接。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(part for part in (block_text(block) for block in content) if part)


def estimate_messages(messages: list[Any]) -> int:
    """整段历史的估算 token（含工具结果）：不做漂移修正，裁剪判据只看绝对值。"""
    return default().count_messages(messages)
