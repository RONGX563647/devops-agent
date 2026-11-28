"""内置模型卡：按 kind 取卡集、按名字匹配。

`list_models()` 每次都 glob + 解析全部 yaml，42 个模型逐个调用就是 42 次重复 IO。
所以卡集按 `(kind, 目录 mtime)` 缓存 —— mtime 是框架升级的自然失效信号，
不需要额外的「重启才生效」说明。
"""

from __future__ import annotations

import inspect
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from devops_agent.model_catalog.kinds import KINDS

#: `-latest` / `-preview`：别名后缀，卡里通常只有基名
_VARIANT_SUFFIX = re.compile(r"-(?:latest|preview)$")
#: 日期后缀：`-2025-04-14` 或 `-20250414`。刻意不含 4 位数字 ——
#: `gpt-4` 会被切成 `gpt`，那是灾难而不是归一化。
_DATE_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def normalize_name(name: str) -> str:
    """小写 + 剥别名/日期后缀。只用于「归一化后相等」这一步，不用于前缀匹配。"""
    out = name.strip().lower()
    out = _VARIANT_SUFFIX.sub("", out)
    return _DATE_SUFFIX.sub("", out)


def _models_dir(kind: str) -> Path | None:
    spec = KINDS.get(kind)
    if spec is None:
        return None
    try:
        module = inspect.getfile(spec.model_cls)
    except (TypeError, OSError):
        return None
    candidate = Path(module).parent / "_models"
    return candidate if candidate.is_dir() else None


def _mtime(kind: str) -> float:
    """目录 mtime 作为卡集版本：框架升级/手工加卡都会改它。"""
    directory = _models_dir(kind)
    if directory is None:
        return 0.0
    try:
        return directory.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=32)
def _load(kind: str, _mtime_key: float) -> tuple[object, ...]:
    """按 kind 加载卡集。`_mtime_key` 只作缓存键，不参与逻辑。"""
    spec = KINDS.get(kind)
    if spec is None:
        return ()
    try:
        # `model_cls` 的静态类型只是 `type`：`list_models()` 是各模型类的类方法，
        # 类型系统看不到。这里的 cast 是本包唯一的「我知道它一定有」。
        loader = cast("Any", spec.model_cls)
        return tuple(loader.list_models())
    except Exception:  # noqa: BLE001 - 卡集读不到不该让整个目录查询失败
        return ()


def cards_for(kind: str) -> list[object]:
    """该 kind 的全部内置卡（进程内缓存，目录变化时自动重载）。"""
    return list(_load(kind, _mtime(kind)))


def match(cards: list[object], model: str) -> tuple[object | None, str]:
    """按「精确 → 最长前缀 → 归一化相等 → 无」匹配，返回 (卡, matched_by)。

    **不做编辑距离/模糊匹配**：`qwen-max-latest` 在卡里确实不存在，猜成
    `qwen-plus` 会给出一个错的窗口，而错的窗口会静默改变压缩阈值 ——
    比显示「未校准」糟得多。
    """
    if not model or not cards:
        return None, "none"
    for card in cards:
        if getattr(card, "name", "") == model:
            return card, "exact"
    prefixed = [card for card in cards if model.startswith(getattr(card, "name", ""))]
    if prefixed:
        # 最长优先；同长按字典序，保证结果与卡集顺序无关
        prefixed.sort(key=lambda card: (-len(getattr(card, "name", "")), getattr(card, "name", "")))
        return prefixed[0], "prefix"
    target = normalize_name(model)
    for card in cards:
        if normalize_name(getattr(card, "name", "")) == target:
            return card, "normalized"
    return None, "none"


def all_card_names(kind: str) -> list[str]:
    """`discover: off` 时的模型清单：纯离线只能列卡里有的。"""
    return [str(getattr(card, "name", "")) for card in cards_for(kind) if getattr(card, "name", "")]
