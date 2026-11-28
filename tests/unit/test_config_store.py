"""配置中心存取层：四层合流、只存差异、乐观锁、审计、只读域。

测试重点不是「能不能写进 SQLite」，而是三条不变量：
1. 覆写只在与下层不同时才落行（否则库里全是历史残留）；
2. env 压过库（容器注入的值必须说了算，且界面要锁定）；
3. 写失败 / 越界 / 拼错键必须吵，不能静默丢。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from devops_agent import config as cfg
from devops_agent import config_store as cs
from devops_agent.config_store import schema, store


@pytest.fixture
def conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """整个 config/ 拷到 tmp，库也指到 tmp：不污染仓库，也不吃本机覆盖。"""
    target = tmp_path / "config"
    shutil.copytree(cfg.config_dir(), target)
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(target))
    monkeypatch.setenv(cs.MODE_ENV, "db")
    monkeypatch.setenv(cs.CONFIG_DB_ENV, str(tmp_path / "config.db"))
    cfg.clear_cache()
    cs.invalidate()
    try:
        yield target
    finally:
        cfg.clear_cache()
        cs.invalidate()


def _rows(domain: str) -> int:
    with schema.connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM config_entries WHERE domain=?", (domain,)
        ).fetchone()
    return int(row["n"])


def test_override_beats_yaml_and_reports_source(conf: Path) -> None:
    # 现造一份 yaml 基线：仓库里已不再保留这些部署 yaml，但「yaml 可评审、
    # 无数据库环境可用」仍是 ADR-0005 的能力，必须继续守住。
    (conf / "memory.yaml").write_text(
        "memory:\n  short_term:\n    max_turns: 7\n", encoding="utf-8"
    )
    cs.invalidate()
    snapshot = cs.resolved("memory")
    assert snapshot.values["short_term"]["max_turns"] == 7
    assert snapshot.sources["short_term.max_turns"] == "yaml"
    # 没被 yaml 覆盖的键必须报 default：来源按「该层自己的键集」判定，
    # 而不是拿合并后的键集 —— 否则代码默认值会被谎报成 yaml 基线。
    assert snapshot.sources["short_term.dedup_reads"] == "default"

    updated = cs.patch("memory", {"short_term.max_turns": 20})
    assert updated.values["short_term"]["max_turns"] == 20
    assert updated.sources["short_term.max_turns"] == "db"
    assert updated.version == 1


def test_same_as_lower_layer_is_not_stored(conf: Path) -> None:
    """写一个与 yaml 相同的值 = 恢复默认，不该在库里留下一行。"""
    snapshot = cs.patch("input", {"fetch_timeout_seconds": 5.0})
    assert snapshot.version == 0
    assert _rows("input") == 0


def test_null_removes_override(conf: Path) -> None:
    cs.patch("input", {"max_references": 9})
    assert _rows("input") == 1
    snapshot = cs.patch("input", {"max_references": None})
    assert snapshot.values["max_references"] == 5
    assert _rows("input") == 0


def test_env_wins_and_is_locked(conf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_MEMORY_SCOPE", "user")
    snapshot = cs.resolved("memory")
    assert snapshot.values["long_term"]["scope"] == "user"
    assert snapshot.sources["long_term.scope"] == "env"
    with pytest.raises(cs.InvalidConfig, match="环境变量锁定"):
        cs.patch("memory", {"long_term.scope": "session"})


def test_invalid_value_is_rejected_without_writing(conf: Path) -> None:
    with pytest.raises(cs.InvalidConfig, match="fetch_timeout_seconds"):
        cs.patch("input", {"fetch_timeout_seconds": 999})
    assert _rows("input") == 0


def test_unknown_key_is_rejected(conf: Path) -> None:
    with pytest.raises(cs.InvalidConfig, match="未知配置键"):
        cs.patch("input", {"fetch_timeout": 3})


def test_dynamic_dict_child_is_allowed(conf: Path) -> None:
    """dict 型字段允许自定义子键（如 health.grades 的分档），不能被当成拼错。"""
    snapshot = cs.patch("health", {"grades.S": 95})
    assert snapshot.values["grades"]["S"] == 95


def test_readonly_domain_cannot_be_written(conf: Path) -> None:
    with pytest.raises(cs.InvalidConfig, match="只读"):
        cs.patch("policy", {"confirm.display_limit": 3000})


def test_version_conflict(conf: Path) -> None:
    cs.patch("input", {"max_references": 7})
    with pytest.raises(cs.ConfigConflict) as caught:
        cs.patch("input", {"max_references": 8}, expected=99)
    assert caught.value.version == 1


def test_file_mode_has_no_database_and_rejects_writes(
    conf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cs.MODE_ENV, "file")
    cs.invalidate()
    assert cs.resolved("input").version == 0
    with pytest.raises(cs.InvalidConfig, match="file 模式"):
        cs.patch("input", {"max_references": 7})


def test_reset_all_clears_domain(conf: Path) -> None:
    cs.patch("health", {"latency_baseline_ms": 4000, "per_step_baseline_ms": 5000})
    assert _rows("health") == 2
    snapshot = cs.reset("health", all_=True)
    assert _rows("health") == 0
    assert snapshot.values["latency_baseline_ms"] == 5000


def test_drift_detected_when_code_default_changed(conf: Path) -> None:
    """覆盖写入时默认是 5000，现在代码默认也是 5000；改成 3000 才算漂移。"""
    cs.patch("health", {"latency_baseline_ms": 4000})
    with schema.connect() as conn:
        conn.execute(
            """UPDATE config_entries SET default_at_write='3000'
               WHERE domain='health' AND key='latency_baseline_ms'"""
        )
    cs.invalidate()
    assert cs.resolved("health").drifted == ["latency_baseline_ms"]


def test_audit_records_old_and_new(conf: Path) -> None:
    cs.patch("input", {"max_references": 9})
    entries = cs.audit("input")
    assert entries[0]["key"] == "max_references"
    assert entries[0]["new"] == 9
    assert entries[0]["version"] == 1


def test_export_only_contains_overrides(conf: Path) -> None:
    cs.patch("input", {"max_references": 9})
    text = cs.export_yaml("input")
    assert "input:" in text
    assert "max_references: 9" in text
    assert "fetch_timeout_seconds" not in text


def test_loader_is_hot_after_patch(conf: Path) -> None:
    """不经 clear_cache，健康分口径也要立刻反映新值（面板改完下一单即生效）。"""
    cs.patch("health", {"latency_baseline_ms": 3500})
    assert cfg.load_health().latency_baseline_ms == 3500


def test_index_lists_every_domain(conf: Path) -> None:
    index = {item["domain"]: item for item in cs.index()}
    assert set(index) == {
        "health",
        "input",
        "memory",
        "gateway",
        "policy",
        "model",
        "sandbox",
        "workspace",
    }
    assert index["policy"]["writable"] is False
    assert index["model"]["managed_by"] == "model_settings"
    assert index["gateway"]["hot"] == "restart"
    assert index["sandbox"]["hot"] == "restart"
    assert index["workspace"]["hot"] == "restart"


def test_bounds_expose_constraints(conf: Path) -> None:
    bounds = store.bounds("input")
    assert bounds["fetch_timeout_seconds"]["min_exclusive"] == 0
    assert bounds["max_references"]["min"] == 1
