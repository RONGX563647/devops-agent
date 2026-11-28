"""能力合并（SPEC-0018 §6）：四源优先级、模型名匹配、校准表、换模型跟随。

这里的重点是「每个数字都能说出从哪来」：只断言值不够 —— 值对了但来源标错，
界面就会在「这个窗口是网关给的」和「这是猜的」之间给出错误解释。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from devops_agent import config as cfg
from devops_agent.model_catalog import registry
from devops_agent.model_catalog.cards import cards_for, match, normalize_name
from devops_agent.model_catalog.catalog import reset as reset_catalog
from devops_agent.model_catalog.merge import find_calibration, merge
from devops_agent.model_catalog.merge import invalidate as merge_invalidate
from devops_agent.model_catalog.schema import DEFAULT_CONTEXT_SIZE, DiscoveryItem
from devops_agent.model_settings import resolve, values_of


@pytest.fixture(autouse=True)
def _clear() -> None:
    registry.invalidate()
    merge_invalidate()
    cfg.clear_cache()
    reset_catalog()
    yield
    registry.invalidate()
    merge_invalidate()
    cfg.clear_cache()
    reset_catalog()


def _config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEVOPS_AGENT_CONFIG_DIR", str(tmp_path))
    registry.invalidate()
    merge_invalidate()
    cfg.clear_cache()
    return tmp_path


def _card(name: str, context: int, output: int = 1024, **extra) -> object:
    """构造一张假卡：匹配与合并的用例不该依赖框架里恰好有哪些模型。"""
    base = {
        "name": name,
        "label": name.upper(),
        "context_size": context,
        "output_size": output,
        "input_types": ["text/plain"],
        "output_types": ["text/plain"],
        "status": "active",
        "parameter_schema": {"type": "object", "properties": {"max_tokens": {"maximum": output}}},
        "deprecated_at": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


# --- A45 匹配规则 ------------------------------------------------------


def test_longest_prefix_wins_on_real_cards() -> None:
    """A45：`gpt-4.1-mini-2025-04-14` 同时是 `gpt-4.1-mini` 与 `gpt-4.1` 的前缀，
    必须命中更长的那个 —— 否则卡集一旦让兄弟模型能力分离，就会静默改掉压缩阈值。
    """
    card, how = match(cards_for("openai_chat"), "gpt-4.1-mini-2025-04-14")
    assert how == "prefix"
    assert card is not None
    assert card.name == "gpt-4.1-mini"


def test_unmatched_model_is_not_guessed() -> None:
    """A45：`qwen-max-latest` 在卡里不存在时不许模糊到 `qwen-plus`。"""
    card, how = match(cards_for("dashscope_chat"), "qwen-max-latest")
    assert card is None
    assert how == "none"


def test_exact_then_normalized() -> None:
    cards = [_card("qwen-plus", 131072), _card("kimi-k2.5", 262144)]
    assert match(cards, "qwen-plus")[1] == "exact"
    # 别名后缀走归一化，而不是靠前缀运气
    assert match(cards, "qwen-plus-latest")[1] == "prefix"
    assert normalize_name("Qwen-Plus-Latest") == "qwen-plus"
    assert normalize_name("gpt-4.1-mini-2025-04-14") == "gpt-4.1-mini"
    # `gpt-4` 不能被切成 `gpt`：日期后缀只认 8 位或 YYYY-MM-DD
    assert normalize_name("gpt-4") == "gpt-4"


def test_card_for_unknown_kind_is_empty() -> None:
    assert cards_for("nope") == []


# --- A38 四源优先级 ----------------------------------------------------


def test_all_four_sources_together() -> None:
    """A38：校准 > 发现 > 卡 > 兜底。三组组合的取值与来源都要对。"""
    from devops_agent.model_catalog.merge import Calibration

    card = _card("m", 1_000_000, output=32_768)
    discovered = DiscoveryItem(id="m", context_size=131_072)

    cal = Calibration(provider="p", name="m", context_size=262_144, output_size=65_536)
    both = merge("p", "m", discovered=discovered, card=card, calibration=cal)
    assert both.context_size == 262_144
    assert both.sources["context_size"] == "calibrated"
    assert both.output_size == 65_536
    assert both.sources["output_size"] == "calibrated"

    no_cal = merge("p", "m", discovered=discovered, card=card, calibration=None, calibrated=False)
    assert no_cal.context_size == 131_072  # 网关更贴近这个部署实例
    assert no_cal.sources["context_size"] == "discovered"
    assert no_cal.output_size == 32_768  # 输出上限则卡更可信
    assert no_cal.sources["output_size"] == "card"

    card_only = merge("p", "m", card=card, calibrated=False)
    assert card_only.context_size == 1_000_000
    assert card_only.sources["context_size"] == "card"


def test_no_source_falls_back_to_default() -> None:
    """A45 的另一半：什么都没有时是 128000 + `framework-default`（= 未校准）。"""
    cap = merge("p", "m", calibrated=False)
    assert cap.context_size == DEFAULT_CONTEXT_SIZE
    assert cap.sources["context_size"] == "framework-default"
    assert cap.matched_by == "none"
    assert cap.parameter_schema == {}


def test_parameter_schema_only_from_card() -> None:
    """A38：`parameter_schema` 只有卡有；发现接口给了也不能冒充。"""
    discovered = DiscoveryItem(id="m", context_size=1000)
    cap = merge("p", "m", discovered=discovered, calibrated=False)
    assert cap.parameter_schema == {}
    assert cap.sources["parameter_schema"] == "framework-default"

    with_card = merge("p", "m", card=_card("m", 1000), calibrated=False)
    assert with_card.parameter_schema["properties"]["max_tokens"]["maximum"] == 1024


def test_label_prefers_gateway_display_name() -> None:
    """网关给了与 id 不同的展示名就用它；否则用卡的 label。"""
    discovered = DiscoveryItem(id="m", label="部署内的 M")
    assert merge("p", "m", discovered=discovered, calibrated=False).label == "部署内的 M"
    assert merge("p", "m", card=_card("m", 1), calibrated=False).label == "M"


# --- 校准表 ------------------------------------------------------------


def test_calibration_four_step_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`local+精确 > main+精确 > local+通配 > main+通配`，顺序固定。"""
    root = _config_dir(tmp_path, monkeypatch)
    (root / "models.yaml").write_text(
        "models:\n"
        "  - {provider: p, name: m, context_size: 111}\n"
        "  - {provider: '*', name: m, context_size: 222}\n"
        "  - {provider: '*', name: only-wild, context_size: 333}\n",
        encoding="utf-8",
    )
    (root / "models.local.yaml").write_text(
        "models:\n"
        "  - {provider: p, name: m, context_size: 444}\n"
        "  - {provider: p, name: wild-local, context_size: 555}\n",
        encoding="utf-8",
    )
    merge_invalidate()

    assert find_calibration("p", "m").context_size == 444  # local 精确
    assert find_calibration("q", "m").context_size == 222  # main 通配
    assert find_calibration("x", "only-wild").context_size == 333


def test_calibration_ignores_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """校准是人填的，**不允许前缀匹配**：否则 `qwen-max` 会误套到 `qwen-max-long`。"""
    root = _config_dir(tmp_path, monkeypatch)
    (root / "models.yaml").write_text(
        "models:\n  - {provider: '*', name: qwen-max, context_size: 131072}\n",
        encoding="utf-8",
    )
    merge_invalidate()
    assert find_calibration("*", "qwen-max") is not None
    assert find_calibration("*", "qwen-max-long") is None


def test_calibration_bad_yaml_reports_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _config_dir(tmp_path, monkeypatch)
    (root / "models.yaml").write_text(
        "models:\n  - {name: m, context_size: 'big'}\n", encoding="utf-8"
    )
    merge_invalidate()
    with pytest.raises(Exception) as exc:
        find_calibration("p", "m")
    assert "context_size" in str(exc.value)


# --- A39 换模型窗口跟随 ------------------------------------------------


def _write_providers(root: Path) -> None:
    (root / "providers.yaml").write_text(
        "providers:\n"
        "  - {id: moonshot, kind: moonshot_chat, base_url: 'https://api.moonshot.cn/v1',"
        " api_key: none, discover: off}\n"
        "default: moonshot\n",
        encoding="utf-8",
    )
    registry.invalidate()
    cfg.clear_cache()
    reset_catalog()


def test_context_follows_model_when_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A39：配了 providers.yaml 时 `context_auto` 默认 true，换模型窗口自动跟随。"""
    root = _config_dir(tmp_path, monkeypatch)
    _write_providers(root)
    from devops_agent.model_catalog import get_catalog

    cat = get_catalog()
    assert cat.default_context_auto() is True

    layer = {"provider": "moonshot", "model": "moonshot-v1-128k"}
    big = values_of(resolve(layer, {}, capability=cat))
    assert big["context_auto"] is True
    assert big["context_size"] == 131_072

    layer["model"] = "moonshot-v1-32k"
    small = values_of(resolve(layer, {}, capability=cat))
    assert small["context_size"] == 32_768

    sources = resolve(layer, {}, capability=cat)
    assert sources["context_size"].source.value == "card"


def test_manual_context_wins_when_auto_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A39 后半：`context_auto=false` 时用户手填值不被覆盖。"""
    root = _config_dir(tmp_path, monkeypatch)
    _write_providers(root)
    from devops_agent.model_catalog import get_catalog

    values = values_of(
        resolve(
            {
                "provider": "moonshot",
                "model": "moonshot-v1-32k",
                "context_auto": False,
                "context_size": 200_000,
            },
            {},
            capability=get_catalog(),
        )
    )
    assert values["context_size"] == 200_000


def test_without_capability_auto_is_forced_false() -> None:
    """A35：没有能力查询器时 `context_auto` 恒 false —— 不声称做不到的事。"""
    values = values_of(resolve({"model": "gpt-4.1"}, {}, capability=None))
    assert values["context_auto"] is False


def test_catalog_disabled_returns_static_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§13 回滚开关 1：`catalog.enabled=false` 时不合并能力，窗口回静态值。"""
    root = _config_dir(tmp_path, monkeypatch)
    (root / "providers.yaml").write_text(
        "providers:\n"
        "  - {id: moonshot, kind: moonshot_chat, base_url: 'https://api.moonshot.cn/v1',"
        " api_key: none, discover: off}\n"
        "catalog: {enabled: false}\n",
        encoding="utf-8",
    )
    registry.invalidate()
    reset_catalog()
    from devops_agent.model_catalog import get_catalog

    cat = get_catalog()
    assert cat.default_context_auto() is False
    assert cat.capability("moonshot", "moonshot-v1-32k") is None
    assert (
        values_of(resolve({"model": "moonshot-v1-32k"}, {}, capability=cat))["context_size"]
        == 128_000
    )
