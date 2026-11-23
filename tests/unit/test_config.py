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
    policy = cfg.load_policy()
    assert policy.react.default == 12
    assert policy.react.delivery == 16
    assert policy.model.max_retries == 3
    # 无人值守服务的默认权限模式：只读放行、其余拒绝，绝不返回 ASK。
    # 若这里被改回 default，敏感调用会退化成「发个确认事件就结束」的静默中断。
    assert policy.permission.mode == "dont_ask"
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


def test_custom_yaml_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text(
        "permission:\n  mode: explore\nreact:\n  default: 7\n  delivery: 9\n"
        "model:\n  max_retries: 5\n",
        encoding="utf-8",
    )
    (tmp_path / "gates.yaml").write_text(
        "quality_gates:\n  coverage_fail_under: 91\n  complexity_threshold: 6\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    policy = cfg.load_policy()
    gates = cfg.load_gates()
    assert policy.permission.mode == "explore"
    assert policy.react.default == 7
    assert policy.react.delivery == 9
    assert policy.model.max_retries == 5
    assert gates.coverage_fail_under == 91
    assert gates.complexity_threshold == 6
    assert gates.mutation_score == 70  # 未声明项回退默认


def test_max_iters_for_fallback() -> None:
    react = cfg.ReactPolicy(default=4, delivery=8)
    assert react.max_iters_for("delivery") == 8
    assert react.max_iters_for("schedule") == 4
    assert react.max_iters_for("model_dump") == 4


def test_invalid_mode_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text("permission:\n  mode: nope\n", encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    with pytest.raises(ValidationError):
        cfg.load_policy()


def test_invalid_yaml_shape_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    with pytest.raises(ValueError, match="映射"):
        cfg.load_policy()
