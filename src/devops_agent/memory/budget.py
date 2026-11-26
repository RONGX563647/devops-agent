"""上下文预算 —— 从模型窗口推导，不另立常量。

旧方案直接取 `context_size × trigger_ratio` 当记忆预算，没扣系统提示、
工具 schema 与输出预留；工具 schema 单项就能上万字符，不扣除会让
「短期 50%」大于真实可用的历史空间。这里把扣除项显式化。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Budget:
    """一次装配的预算分配（单位 token）。"""

    window: int
    threshold: int
    fixed: int
    output_reserve: int
    history_max: int
    short_term: int
    episodic: int
    long_term: int
    headroom: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _share(total: int, ratio: float) -> int:
    return max(0, int(total * ratio))


def compute_budget(
    *,
    context_size: int,
    trigger_ratio: float,
    reserve_ratio: float,
    max_tokens: int | None,
    prefix_tokens: int,
    short_term_ratio: float,
    episodic_ratio: float,
    long_term_ratio: float,
    headroom_ratio: float,
    episodic_enabled: bool = True,
    long_term_enabled: bool = True,
) -> Budget:
    """按「窗口 − 固定前缀 − 输出预留」推导历史面额度，再按比例分账。

    1) 阈值与窗口同源：threshold = trigger_ratio × window；
    2) 输出预留优先取 max_tokens，否则用 reserve_ratio × window；
    3) 未启用的层把份额并给短期，不重新定义比例。
    """
    window = max(0, int(context_size))
    threshold = _share(window, trigger_ratio)
    output = max(0, int(max_tokens)) if max_tokens else _share(window, reserve_ratio)
    fixed = max(0, int(prefix_tokens))
    history_max = max(0, threshold - fixed - output)

    short = short_term_ratio
    episodic = episodic_ratio if episodic_enabled else 0.0
    long_term = long_term_ratio if long_term_enabled else 0.0
    # 被关掉的层把份额交给短期：比例本身不重写，运行时才有可解释的对照
    short += 0.0 if episodic_enabled else episodic_ratio
    short += 0.0 if long_term_enabled else long_term_ratio

    return Budget(
        window=window,
        threshold=threshold,
        fixed=fixed,
        output_reserve=output,
        history_max=history_max,
        short_term=_share(history_max, short),
        episodic=_share(history_max, episodic),
        long_term=_share(history_max, long_term),
        headroom=_share(history_max, headroom_ratio),
    )
