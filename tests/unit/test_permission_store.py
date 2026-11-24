"""规则文件测试：容错、去重、原子写、信任目录。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from devops_agent.governance import store
from devops_agent.governance.policy import compile_context
from devops_agent.governance.profiles import PROFILES
from devops_agent.governance.rules import Rule


def _rule(pattern: str = "pytest:*", **kwargs: object) -> Rule:
    data: dict[str, object] = {
        "tool": "Bash",
        "pattern": pattern,
        "behavior": "allow",
        "source": "user",
        "scope": "workspace",
    }
    data.update(kwargs)
    return Rule(**data)  # type: ignore[arg-type]


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "nope.yaml"
    assert store.local_rules(path) == []
    assert store.trust_config(path).mode == "permissive"


def test_corrupt_file_is_skipped_without_raising(tmp_path: Path) -> None:
    """配置文件写坏不该让服务起不来 —— 沿用 available() 的宽容原则。"""
    path = tmp_path / "bad.yaml"
    path.write_text("rules: [", encoding="utf-8")
    assert store.local_rules(path) == []
    # 上下文仍然能编译出来，只是少了本地规则
    assert compile_context(PROFILES["workspace"]).mode.value == "accept_edits"


def test_invalid_entries_are_skipped_one_by_one(tmp_path: Path) -> None:
    path = tmp_path / "mixed.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {"tool": "Bash", "pattern": "pytest:*", "behavior": "allow"},
                    {"tool": "", "pattern": "x", "behavior": "allow"},  # 缺工具名
                    {"tool": "Bash", "pattern": "x", "behavior": "maybe"},  # 非法行为
                    {"tool": "Bash", "pattern": "y", "behavior": "allow", "scope": "orbit"},
                    "not-a-mapping",
                ]
            }
        ),
        encoding="utf-8",
    )
    rules = store.local_rules(path)
    assert [rule.pattern for rule in rules] == ["pytest:*"]


def test_add_rule_overwrites_same_identity(tmp_path: Path) -> None:
    """同一 (tool, pattern) 覆盖而不是追加：规则列表不能无限长。"""
    path = tmp_path / "rules.yaml"
    store.add_rule(_rule("pytest:*", origin="first"), path)
    stored = store.add_rule(_rule("pytest:*", origin="second"), path)
    assert len(stored) == 1
    assert stored[0].origin == "second"
    assert stored[0].created_at  # 写入时自动补时间戳


def test_remove_rule_reports_whether_it_removed(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    store.add_rule(_rule("pytest:*"), path)
    assert store.remove_rule("Bash", "pytest:*", path) is True
    assert store.remove_rule("Bash", "pytest:*", path) is False
    assert store.local_rules(path) == []


def test_written_file_is_valid_yaml_and_has_no_temp_leftovers(tmp_path: Path) -> None:
    """原子替换：目录里只应有目标文件，不该留 .tmp。"""
    path = tmp_path / "rules.yaml"
    store.add_rule(_rule("pytest:*"), path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["rules"][0]["pattern"] == "pytest:*"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["rules.yaml"]


def test_trust_mode_defaults_permissive_and_strict_checks_workdir(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    assert store.trust_config(path).is_trusted(tmp_path) is True  # 默认不校验

    trusted = tmp_path / "repo"
    trusted.mkdir()
    store.trust_workdir(trusted, path)
    path.write_text(
        yaml.safe_dump(
            {
                "rules": [],
                "trust": {"mode": "strict", "workdirs": [str(trusted.resolve())]},
            }
        ),
        encoding="utf-8",
    )
    config = store.trust_config(path)
    assert config.mode == "strict"
    assert config.is_trusted(trusted) is True
    assert config.is_trusted(tmp_path / "other") is False


def test_unknown_trust_mode_falls_back_to_permissive(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"trust": {"mode": "yolo"}}), encoding="utf-8")
    assert store.trust_config(path).mode == "permissive"


def test_trust_workdir_keeps_existing_rules(tmp_path: Path) -> None:
    """信任与规则共用一个文件：写信任不能把规则清掉。"""
    path = tmp_path / "rules.yaml"
    store.add_rule(_rule("pytest:*"), path)
    store.trust_workdir(tmp_path, path)
    assert [rule.pattern for rule in store.local_rules(path)] == ["pytest:*"]
    assert store.trust_config(path).workdirs


@pytest.mark.parametrize("behavior", ["allow", "ask", "deny"])
def test_all_behaviors_round_trip(tmp_path: Path, behavior: str) -> None:
    path = tmp_path / "rules.yaml"
    store.add_rule(_rule("x:*", behavior=behavior), path)
    assert store.local_rules(path)[0].behavior == behavior
