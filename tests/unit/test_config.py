"""配置加载层单元测试：默认值、环境变量覆盖、按域 max_iters、非法值校验。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from devops_agent import config as cfg


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(cfg.CONFIG_DIR_ENV, raising=False)
    cfg.clear_cache()
    yield
    cfg.clear_cache()


def test_config_dir_default() -> None:
    assert cfg.config_dir().name == "config"


def test_config_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    assert cfg.config_dir() == tmp_path.resolve()


def test_load_policy_from_repo() -> None:
    """policy.yaml 现在只剩确认协议；权限档位已迁到 governance/model.py。"""
    policy = cfg.load_policy()
    assert policy.confirm.display_limit == 2000
    assert policy.confirm.rules.scopes == ["once", "exact", "prefix"]
    assert cfg.load_policy() is policy  # 缓存命中


def test_load_gates_from_repo() -> None:
    gates = cfg.load_gates()
    assert gates.coverage_fail_under == 80
    assert gates.complexity_threshold == 10
    assert gates.mutation_score == 70
    assert gates.dead_code_min_confidence == 80


def test_missing_files_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    assert cfg.load_policy() == cfg.RuntimePolicy()
    assert cfg.load_gates() == cfg.QualityGates()
    assert cfg.load_model() == cfg.ModelSettings()


def test_custom_yaml_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text(
        "confirm:\n  display_limit: 1234\n  rules:\n    scopes: [once]\n", encoding="utf-8"
    )
    (tmp_path / "model.yaml").write_text(
        "model:\n  max_iters:\n    default: 7\n    delivery: 9\n  agent_max_retries: 5\n",
        encoding="utf-8",
    )
    (tmp_path / "gates.yaml").write_text(
        "quality_gates:\n  coverage_fail_under: 91\n  complexity_threshold: 6\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    policy = cfg.load_policy()
    gates = cfg.load_gates()
    model = cfg.load_model()
    assert policy.confirm.display_limit == 1234
    assert policy.confirm.rules.scopes == ["once"]
    assert model.max_iters_for("schedule") == 7
    assert model.max_iters_for("delivery") == 9
    assert model.agent_max_retries == 5
    assert gates.coverage_fail_under == 91
    assert gates.complexity_threshold == 6
    assert gates.mutation_score == 70  # 未声明项回退默认


def test_max_iters_for_fallback() -> None:
    settings = cfg.ModelSettings(max_iters={"default": 4, "delivery": 8})
    assert settings.max_iters_for("delivery") == 8
    assert settings.max_iters_for("schedule") == 4
    assert settings.max_iters_for("model_dump") == 4


def test_invalid_confirm_value_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """确认协议的域知识仍在加载期校验：越界值不该等到用户点确认才炸。

    权限段已迁到 governance/model.py（代码常量，由 mypy + 单测覆盖），
    所以这里换成 confirm 段的非法值。
    """
    (tmp_path / "policy.yaml").write_text("confirm:\n  display_limit: 10\n", encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    with pytest.raises(ValidationError):
        cfg.load_policy()


def test_invalid_yaml_shape_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    with pytest.raises(ValueError, match="映射"):
        cfg.load_policy()
