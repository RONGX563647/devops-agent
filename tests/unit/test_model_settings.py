"""模型参数解析器单测：优先级、来源标注、校验、派生值、参数指纹。

这一层的契约很硬，值得逐条钉住：
- **优先级**：会话覆盖 > 部署默认 > yaml > env > 框架默认，顺序错了就会
  「界面上改了但实际没用上」；
- **来源**：每个值都要能说出是谁设的。缺陷一（context_size 靠猜）之所以
  藏了半年，就是因为没有来源标注；
- **派生**：压缩阈值与最坏尝试次数只能由后端算，前端重复实现迟早对不上。
"""

from __future__ import annotations

import pytest

from devops_agent import config as cfg
from devops_agent import model_settings as ms


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都从干净的配置缓存与空 DB 开始。"""
    monkeypatch.delenv(ms.MODEL_ENV, raising=False)
    cfg.clear_cache()
    ms.invalidate()
    yield
    cfg.clear_cache()
    ms.invalidate()


def test_yaml_values_carry_yaml_source() -> None:
    """model.yaml 里显式写过的项来源是 yaml，没写的才是 framework-default。"""
    resolved = ms.resolve(stored={})
    assert resolved["context_size"].value == 128_000
    assert resolved["context_size"].source is ms.Source.YAML
    assert resolved["max_iters"].value == 12


def test_env_supplies_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """yaml 的 name 为空时用环境变量 —— 换模型不该改代码。"""
    monkeypatch.setenv(ms.MODEL_ENV, "qwen-from-env")
    resolved = ms.resolve(stored={})
    assert resolved["model"] == ms.Resolved("qwen-from-env", ms.Source.ENV)


def test_framework_default_source(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置文件缺失时全部回落框架默认，来源要如实标成 framework-default。"""
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    cfg.clear_cache()
    resolved = ms.resolve(stored={})
    assert resolved["context_size"].value == 128_000
    assert resolved["context_size"].source is ms.Source.FRAMEWORK
    assert resolved["trigger_ratio"].source is ms.Source.FRAMEWORK


def test_precedence_session_over_stored_over_yaml() -> None:
    """五层链路的顺序：会话覆盖压过部署默认，部署默认压过 yaml。"""
    resolved = ms.resolve(
        session={"temperature": 0.1, "max_iters": 40},
        stored={"temperature": 0.5, "max_iters": 20, "top_p": 0.9},
    )
    assert resolved["temperature"] == ms.Resolved(0.1, ms.Source.SESSION)
    assert resolved["max_iters"] == ms.Resolved(40, ms.Source.SESSION)
    assert resolved["top_p"] == ms.Resolved(0.9, ms.Source.STORED)
    # 两层都没提的项仍是 yaml
    assert resolved["reserve_ratio"].source is ms.Source.YAML


def test_null_in_layer_falls_through() -> None:
    """`None` 表示「用下层默认」，不是「设成 null 值」。"""
    resolved = ms.resolve(session={"temperature": None}, stored={"temperature": 0.7})
    assert resolved["temperature"] == ms.Resolved(0.7, ms.Source.STORED)


def test_apply_patch_merges_and_deletes() -> None:
    base = {"temperature": 0.3, "max_iters": 20}
    assert ms.apply_patch(base, {"temperature": None}) == {"max_iters": 20}
    assert ms.apply_patch(base, {"top_p": 0.5})["top_p"] == 0.5


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ms.InvalidConfig) as caught:
        ms.apply_patch({}, {"api_key": "sk-xxx"})
    assert caught.value.field == "api_key"


def test_out_of_range_is_rejected() -> None:
    values = ms.values_of(ms.resolve(stored={}))
    for bad in ({"temperature": 3}, {"max_iters": 0}, {"trigger_ratio": 0.95}):
        with pytest.raises(ms.InvalidConfig):
            ms.validate({**values, **bad})


def test_cross_field_rules() -> None:
    """联动约束：单独看都合法、合起来必错的那种。"""
    values = ms.values_of(ms.resolve(stored={}))
    with pytest.raises(ms.InvalidConfig):
        ms.validate({**values, "reserve_ratio": 0.85, "trigger_ratio": 0.5})
    with pytest.raises(ms.InvalidConfig):
        ms.validate({**values, "max_tokens": 200_000, "context_size": 128_000})


def test_warnings_are_not_errors() -> None:
    """最坏尝试次数与长迭代只警告：它们是成本提示，不是非法值。"""
    values = ms.values_of(ms.resolve(stored={}))
    hints = ms.validate(values)
    assert any("16 次模型请求" in hint for hint in hints)
    hints = ms.validate({**values, "max_iters": 120})
    assert any("120 轮" in hint for hint in hints)


def test_derived_values() -> None:
    """压缩阈值与最坏尝试次数由后端算，前端不重复实现口径。"""
    resolved = ms.resolve(stored={})
    derived = ms.derived(ms.values_of(resolved), ms.sources_of(resolved))
    assert derived["compression_threshold"] == 102_400
    assert derived["worst_case_attempts"] == 16
    # 128000 来自 yaml 且没被确认过 → 未校准
    assert derived["context_uncalibrated"] is True


def test_setting_context_size_marks_it_calibrated() -> None:
    """部署者在界面上亲手填过窗口，就不再是「猜的」。"""
    resolved = ms.resolve(stored={"context_size": 200_000})
    derived = ms.derived(ms.values_of(resolved), ms.sources_of(resolved))
    assert derived["compression_threshold"] == 160_000
    assert derived["context_uncalibrated"] is False


def test_params_record_keeps_only_non_defaults() -> None:
    """run 记录只存非默认值：全量记录会让默认值一变就像「参数变了」。"""
    params = ms.ModelParams.from_values(ms.values_of(ms.resolve(stored={})))
    record = params.record()
    # 只有模型名（非空时）算非默认；这里是空的，所以记录为空
    assert record == {}
    changed = ms.ModelParams.from_values(
        ms.values_of(ms.resolve(stored={"temperature": 0.3, "max_iters": 30}))
    ).record()
    assert changed == {"temperature": 0.3, "max_iters": 30}


def test_params_sha_is_stable_and_sensitive() -> None:
    a = ms.ModelParams.from_values({"temperature": 0.3})
    b = ms.ModelParams.from_values({"temperature": 0.3})
    c = ms.ModelParams.from_values({"temperature": 0.4})
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_bounds_payload_shape() -> None:
    bounds = ms.bounds_payload()
    assert bounds["temperature"]["max"] == 2
    assert bounds["context_size"]["min"] == 8192
    assert "choices" in bounds["reasoning_effort"]
