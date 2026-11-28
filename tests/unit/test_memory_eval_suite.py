"""记忆题库解析、计划加载与变体派生（SPEC-0014 §5/§12）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.openjudge.memory import suite
from devops_agent.openjudge.memory.variants import config_sha, variant_policy


def _case(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "mt-001",
        "track": "MT",
        "tier": "must_pass",
        "memory_required": True,
        "sessions": [
            {
                "id": "s1",
                "turns": [
                    {"text": "请记住：时区是 Asia/Shanghai。", "remember": True},
                    {"text": "时区是什么？", "remember": False},
                ],
            }
        ],
        "probes": [
            {
                "after": "s1",
                "question": "时区是什么？",
                "kind": "recall",
                "expected_contains": ["Asia/Shanghai"],
            }
        ],
        "gold": ["Asia/Shanghai"],
    }
    base.update(overrides)
    return base


def test_parse_case_roundtrip() -> None:
    case = suite.parse_case(_case())
    assert case.id == "mt-001"
    assert case.sessions[0].turns[0].remember is True
    assert case.probes[0].expected_contains == ("Asia/Shanghai",)
    assert suite.validate([case]) == []


def test_parse_case_rejects_unknown_probe_session() -> None:
    raw = _case(probes=[{"after": "s9", "question": "q", "expected_contains": ["x"]}])
    with pytest.raises(ValueError):
        suite.parse_case(raw)


def test_validate_flags_missing_gold_and_empty_isolation() -> None:
    raw = _case(gold=[])
    case = suite.parse_case(raw)
    problems = suite.validate([case])
    assert any("gold" in item for item in problems)
    isolation = suite.parse_case(
        _case(
            track="NB",
            memory_required=False,
            probes=[{"after": "s1", "question": "q", "kind": "isolation"}],
        )
    )
    assert any("expected_absent" in item for item in suite.validate([isolation]))


def test_suite_sha_is_stable_and_order_independent() -> None:
    first = suite.parse_case(_case(id="a"))
    second = suite.parse_case(_case(id="b"))
    assert suite.suite_sha([first, second]) == suite.suite_sha([second, first])
    changed = suite.parse_case(_case(id="a", gold=["UTC"]))
    assert suite.suite_sha([first]) != suite.suite_sha([changed])


def test_load_cases_reads_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "cases" / "memory.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_case(), ensure_ascii=False) + "\n", encoding="utf-8")
    cases = suite.load_cases(path.parent)
    assert [case.id for case in cases] == ["mt-001"]


def test_load_plan_treats_unquoted_off_as_variant_name(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        "plan: p\nsuite: cases\nbaseline: off\nprimary: full\n"
        "variants:\n  off: {}\n  full: {}\nmin_n: 1\n",
        encoding="utf-8",
    )
    (tmp_path / "cases").mkdir()
    plan = suite.load_plan(plan_path)
    assert plan.baseline == "off"
    assert "off" in plan.variants
    assert plan.primary == "full"


def test_variant_policy_switches_layers() -> None:
    off = variant_policy("off", "scope")
    assert off.enabled is False
    assert off.episodic.enabled is False
    assert off.long_term.enabled is False
    full = variant_policy("full", "scope")
    assert full.enabled is True
    assert full.episodic.enabled is True and full.long_term.enabled is True
    short = variant_policy("short", "scope")
    assert short.long_term.enabled is False
    episodic = variant_policy("episodic_only", "scope")
    assert episodic.episodic.enabled is True and episodic.long_term.enabled is False


def test_config_sha_is_deterministic() -> None:
    assert config_sha("full", {}) == config_sha("full", {})
    assert config_sha("full", {}) != config_sha("off", {"long_term": False})
