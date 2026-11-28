"""四源逐字段合并 + 校准表。

为什么不整块取一个源：`parameter_schema` 只有内置卡有，而窗口是网关更准
（卡可能滞后于新模型）。整块取一个源必然丢掉另一半 —— 于是出现「窗口对了
但参数范围是默认的」或「参数范围对了但窗口还是 128k」。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from devops_agent.config_files import config_dir, read_yaml
from devops_agent.model_catalog.cards import normalize_name
from devops_agent.model_catalog.schema import (
    DEFAULT_CONTEXT_SIZE,
    Capability,
    DiscoveryItem,
    ProviderError,
)

CALIBRATION_FILE = "models.yaml"
CALIBRATION_LOCAL = "models.local.yaml"

#: 校准表里代表「对所有 provider 生效」的写法
ANY_PROVIDER = frozenset({"", "*"})


@dataclass(frozen=True, slots=True)
class Calibration:
    """一条人工校准值。`name` 按归一化比较，但**不**允许前缀匹配。"""

    provider: str
    name: str
    context_size: int | None = None
    output_size: int | None = None
    note: str = ""


def _parse_int(raw: Any, where: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProviderError(where, "应为整数")
    return raw


def _parse_entry(raw: Any, index: int, path: str) -> Calibration:
    where = f"{path}.models[{index}]"
    if not isinstance(raw, dict):
        raise ProviderError(where, "应为映射")
    name = raw.get("name")
    if not name:
        raise ProviderError(f"{where}.name", "必填项缺失")
    return Calibration(
        provider=str(raw.get("provider") or ""),
        name=str(name),
        context_size=_parse_int(raw.get("context_size"), f"{where}.context_size"),
        output_size=_parse_int(raw.get("output_size"), f"{where}.output_size"),
        note=str(raw.get("note") or ""),
    )


def _parse_file(name: str) -> tuple[Calibration, ...]:
    raw = read_yaml(name)
    entries = raw.get("models") or []
    if not isinstance(entries, list):
        raise ProviderError(f"{name}.models", "应为列表")
    return tuple(_parse_entry(item, index, name) for index, item in enumerate(entries))


def _mtime(name: str) -> float:
    path: Path = config_dir() / name
    try:
        return path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        return 0.0


@lru_cache(maxsize=8)
def _load_cached(
    directory: str, main_mtime: float, local_mtime: float
) -> tuple[tuple[Calibration, ...], tuple[Calibration, ...]]:
    """返回 (主表, local 表)。缓存键含两个 mtime：改任一份都立刻生效。"""
    _ = directory, main_mtime, local_mtime
    return _parse_file(CALIBRATION_FILE), _parse_file(CALIBRATION_LOCAL)


def load_calibrations() -> tuple[tuple[Calibration, ...], tuple[Calibration, ...]]:
    directory = str(config_dir())
    try:
        return _load_cached(directory, _mtime(CALIBRATION_FILE), _mtime(CALIBRATION_LOCAL))
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - yaml 结构错误转成配置错误
        raise ProviderError(CALIBRATION_FILE, f"加载失败：{type(exc).__name__}: {exc}") from exc


def invalidate() -> None:
    _load_cached.cache_clear()


def _same_name(left: str, right: str) -> bool:
    return left == right or normalize_name(left) == normalize_name(right)


def _find_in(
    entries: tuple[Calibration, ...], provider: str | None, model: str
) -> Calibration | None:
    """`provider=None` 表示「通配轮」：只认 `""` / `"*"` 的行。

    不能把 None 当成「任何 provider 都行」—— 那会让第 3 步重新命中第 1 步的
    具名条目，四步回退顺序退化成两步（实测就是这么错的）。
    """
    for entry in entries:
        if provider is None:
            if entry.provider not in ANY_PROVIDER:
                continue
        elif entry.provider != provider:
            continue
        if _same_name(entry.name, model):
            return entry
    return None


def find_calibration(provider_id: str, model: str) -> Calibration | None:
    """四步逐级回退：local+精确 > main+精确 > local+通配 > main+通配。

    顺序固定是必须的：同一条 `qwen3-max` 在 local 与 main 各有一行时，
    「谁生效」必须有唯一答案，否则两个人改同一模型会互相看不见。
    """
    main, local = load_calibrations()
    for entries, provider in (
        (local, provider_id),
        (main, provider_id),
        (local, None),
        (main, None),
    ):
        found = _find_in(entries, provider, model)
        if found is not None:
            return found
    return None


def _pick(*pairs: tuple[Any, str]) -> tuple[Any, str]:
    """按顺序取第一个非 None 的值，并带回它的来源标签。"""
    for value, source in pairs:
        if value is not None:
            return value, source
    return None, ""


def _get(obj: object | None, field: str) -> Any:
    """安全取属性：`None` 与「这个形态没有该字段」都当没写。

    有了它，下面每处取值都不用再写 `x.field if x else None` —— 那种写法会把
    一个字段一条分支地堆进 `merge()`，圈复杂度直接顶到上限（实测 22）。
    """
    return getattr(obj, field, None) if obj is not None else None


def _types(raw: Any, fallback: tuple[str, ...]) -> tuple[str, ...] | None:
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(item) for item in raw)
    return fallback or None


def _types_source(
    card: object | None, discovered: object | None, field: str
) -> tuple[tuple[str, ...], str]:
    """模态：卡优先（卡里写的是这个模型的真实模态），其次网关声明，最后纯文本。"""
    picked, source = _pick(
        (_types(_get(card, field), ()), "card"),
        (_types(_get(discovered, field), ()), "discovered"),
        (("text/plain",), "framework-default"),
    )
    return tuple(picked or ("text/plain",)), source


def _context_source(
    cal: Calibration | None, discovered: object | None, card: object | None
) -> tuple[int, str]:
    """窗口：校准 > 网关声明 > 内置卡 > 兜底。

    网关排在卡前面是刻意的：卡是随框架发版的快照，而网关知道它此刻真实给多少。
    """
    picked, source = _pick(
        (_get(cal, "context_size"), "calibrated"),
        (_get(discovered, "context_size"), "discovered"),
        (_get(card, "context_size"), "card"),
        (DEFAULT_CONTEXT_SIZE, "framework-default"),
    )
    return int(picked), source


def _output_source(
    cal: Calibration | None, discovered: object | None, card: object | None
) -> tuple[int | None, str]:
    """输出上限：校准 > 卡 > 网关声明。

    与窗口相反，卡排在网关前面：`/models` 偶尔把上下窗口误填在输出上限上，
    而卡是人工校对过的。
    """
    picked, source = _pick(
        (_get(cal, "output_size"), "calibrated"),
        (_get(card, "output_size"), "card"),
        (_get(discovered, "output_size"), "discovered"),
    )
    return (int(picked) if picked is not None else None), source


def _schema_source(card: object | None) -> tuple[dict[str, object], str]:
    """参数 schema 只有内置卡有：网关的 `/models` 不提供参数范围。"""
    raw = _get(card, "parameter_schema")
    if isinstance(raw, dict) and raw:
        return raw, "card"
    return {}, "framework-default"


def _label_source(discovered: object | None, card: object | None, model: str) -> tuple[str, str]:
    """展示名：网关给的（且不等于 id）> 卡的 label > 模型 id 本身。"""
    discovered_label = str(_get(discovered, "label") or "")
    if discovered_label and discovered_label != model:
        return discovered_label, "discovered"
    card_label = _get(card, "label")
    if card_label:
        return str(card_label), "card"
    return model, "framework-default"


def _deprecation(card: object | None) -> str:
    value = _get(card, "deprecated_at")
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value or "")


def _calibration(
    provider_id: str, model: str, calibration: Calibration | None, calibrated: bool
) -> Calibration | None:
    """`calibrated=False` 时整段跳过：回滚路径只认静态值，不认校准表。"""
    if not calibrated:
        return None
    if calibration is not None:
        return calibration
    return find_calibration(provider_id, model)


def merge(
    provider_id: str,
    model: str,
    *,
    discovered: DiscoveryItem | None = None,
    card: object | None = None,
    matched_by: str = "none",
    calibration: Calibration | None = None,
    calibrated: bool = True,
) -> Capability:
    """四源逐字段合并成一张能力卡。

    每个字段各自说明「这个数字从哪来」；整块取一个源必然丢掉另一半，
    于是出现「窗口对了但参数范围是默认的」这类看起来没问题的错。
    """
    cal = _calibration(provider_id, model, calibration, calibrated)
    context_size, context_src = _context_source(cal, discovered, card)
    output_size, output_src = _output_source(cal, discovered, card)
    input_types, input_src = _types_source(card, discovered, "input_types")
    output_types, output_types_src = _types_source(card, discovered, "output_types")
    parameter_schema, schema_src = _schema_source(card)
    label, label_src = _label_source(discovered, card, model)
    status, status_src = _pick(
        (_get(card, "status"), "card"),
        ("implicitly_active", "framework-default"),
    )
    matched_card = _get(card, "name")
    return Capability(
        context_size=context_size,
        output_size=output_size,
        input_types=input_types,
        output_types=output_types,
        status=str(status),
        label=label,
        parameter_schema=parameter_schema,
        sources={
            "context_size": context_src,
            "output_size": output_src,
            "input_types": input_src,
            "output_types": output_types_src,
            "parameter_schema": schema_src,
            "label": label_src,
            "status": status_src,
        },
        matched_by=matched_by,
        matched_card=str(matched_card) if matched_card is not None else None,
        deprecated_at=_deprecation(card),
    )
