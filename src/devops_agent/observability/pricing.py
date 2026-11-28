"""模型单价与成本换算 —— 单价写在 `config/model_prices.yaml`，不硬编码。

为什么单独成模块：成本是「单价 × 用量」，用量已经在 spans 里逐条记了
（输入/输出/缓存分开），缺的只是单价。把单价做成配置文件，
换模型或调价不需要改代码，也不会把某家厂商的价目表写进仓库。

未配置单价的模型**不计算成本**（返回 None），而不是按 0 元计入 ——
「没配价格」和「免费」是两件事，混在一起会把总成本算低。
"""

from __future__ import annotations

from dataclasses import dataclass

from devops_agent.config_files import read_yaml

PRICES_FILE = "model_prices.yaml"
# 单价口径：每百万 token 多少元。各厂商价目表都是这个口径，换算只需除以它。
DEFAULT_UNIT_TOKENS = 1_000_000
DEFAULT_CURRENCY = "CNY"


@dataclass(frozen=True, slots=True)
class PriceRule:
    """一个模型的单价（元 / 百万 token）。"""

    input: float
    output: float


@dataclass(frozen=True, slots=True)
class PriceTable:
    """全部单价规则 + 计价口径；`rules` 里没有的模型视为「未配置」。"""

    currency: str
    unit_tokens: int
    rules: dict[str, PriceRule]

    def price_of(self, model: str) -> PriceRule | None:
        """按模型名取单价：先精确匹配，再忽略大小写，最后试默认规则。"""
        rule = self.rules.get(model)
        if rule is not None:
            return rule
        lowered = model.strip().lower()
        for name, candidate in self.rules.items():
            if name.strip().lower() == lowered:
                return candidate
        return self.rules.get("*")

    def cost_of(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        """算一次（或一组）调用的成本；模型没配单价时返回 None。"""
        rule = self.price_of(model)
        if rule is None:
            return None
        unit = self.unit_tokens or DEFAULT_UNIT_TOKENS
        return round(
            (max(0, input_tokens) * rule.input + max(0, output_tokens) * rule.output) / unit,
            6,
        )


def _rule_of(raw: object) -> PriceRule | None:
    """把 yaml 里的一项转成 PriceRule；结构不对就当没配（不让看板 500）。"""
    if not isinstance(raw, dict):
        return None
    try:
        return PriceRule(
            input=float(raw.get("input", 0) or 0),
            output=float(raw.get("output", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


def load_prices() -> PriceTable:
    """读配置；文件缺失/为空时返回空表（所有模型都算「未配置」）。"""
    raw = read_yaml(PRICES_FILE)
    models = raw.get("models")
    rules: dict[str, PriceRule] = {}
    if isinstance(models, dict):
        for name, item in models.items():
            rule = _rule_of(item)
            if rule is not None:
                rules[str(name)] = rule
    # 顶层 default 是兜底规则：字段名用 `*` 存，price_of 最后才回落到它
    fallback = _rule_of(raw.get("default"))
    if fallback is not None:
        rules["*"] = fallback
    unit = raw.get("unit_tokens", DEFAULT_UNIT_TOKENS)
    return PriceTable(
        currency=str(raw.get("currency", DEFAULT_CURRENCY)),
        unit_tokens=int(unit) if isinstance(unit, int) and unit > 0 else DEFAULT_UNIT_TOKENS,
        rules=rules,
    )
