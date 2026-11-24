"""`eval run` 退出码语义的单测。

锁一个已证实的缺陷：`warn`（与基线比退化超过精度上限）此前退出 0，
于是「-20pp 回归」在 nightly 里是绿的。

`cmd_run` 需要模型凭据，无法在单测里跑完整流程，所以测两件事：
1. `_BLOCKING_VERDICTS` 的定义（哪些结论必须红）；
2. `report.compare` 在该退化场景下确实给出 `warn`，
   从而「warn 属于阻断集合」等价于「这个场景会让 nightly 变红」。
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

import devops_agent.eval.cli as cli_mod
from devops_agent.eval.cli import _BLOCKING_VERDICTS
from devops_agent.eval.dataset import Manifest, Suite
from devops_agent.eval.report import compare


def _report(pass_rate: float, prompt_sha: str) -> dict[str, Any]:
    return {
        "n": 40,
        "pass_rate": pass_rate,
        "hard_failures": {},
        "gates": [],
        "metrics": [],
        "mean_trajectory": 0.9,
        "mean_tokens": 100,
        "p50_latency_ms": 1000,
        "mean_judge": None,
        "suite_sha": "aaa",
        "grader_version": "v2",
        "prompt_sha": prompt_sha,
        "model": "m",
        "profile": "per-case",
        "judge_model": "jm",
        "judge_scope": "all",
    }


def test_blocking_verdicts_are_fail_and_warn() -> None:
    """这条集合就是「哪些结论必须让 CI 变红」的唯一定义。"""
    assert _BLOCKING_VERDICTS == {"fail", "warn"}


def test_insufficient_is_not_blocking() -> None:
    """题库没到门禁样本量是正常状态，不能红。"""
    assert "insufficient" not in _BLOCKING_VERDICTS


def test_pass_is_not_blocking() -> None:
    assert "pass" not in _BLOCKING_VERDICTS


def test_minus_20pp_regression_yields_warn_and_is_blocking() -> None:
    """核心场景：通过率 0.90 -> 0.70（−20pp），必须落在阻断集合里。

    旧实现 `return 1 if verdict == "fail" else 0` 会让这个场景退 0。
    """
    baseline = _report(0.90, "p1")
    head = _report(0.70, "p2")
    result = compare(head, baseline)
    assert result["verdict"] == "warn"
    assert result["compatibility"]["level"] == "clean"
    assert result["mde"] <= 0.2
    assert result["verdict"] in _BLOCKING_VERDICTS


def test_hard_failure_yields_fail_and_is_blocking() -> None:
    head = _report(0.9, "p1")
    head["hard_failures"] = {"safety": 1}
    result = compare(head, _report(0.9, "p1"))
    assert result["verdict"] == "fail"
    assert result["verdict"] in _BLOCKING_VERDICTS


def test_identical_reports_are_not_blocking() -> None:
    """复测无变化不该让 nightly 变红。"""
    payload = _report(0.9, "p1")
    result = compare(payload, dict(payload))
    assert result["verdict"] not in _BLOCKING_VERDICTS


def test_incomparable_baseline_returns_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """suite_sha 不同 -> not_comparable -> 必须 exit 2，而不是静默退 0。

    走完整 `cmd_run` 流程，只把运行/落库/写报告换成桩：
    真正要锁的是「不可比」这条结论的退出码。
    """
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    suite = Suite(
        manifest=Manifest(
            suite="demo",
            version="1",
            sha256="head-sha",
            counts={},
            min_cases_for_gate=1,
        ),
        cases=[],
    )
    monkeypatch.setattr(cli_mod, "load_suite", lambda path: suite)
    monkeypatch.setattr(cli_mod, "_init_judge", lambda enabled: (None, ""))
    monkeypatch.setattr(cli_mod, "_runtime_factory", lambda profile: lambda case, workdir: None)
    monkeypatch.setattr(cli_mod, "_persist", lambda *args, **kwargs: 1)
    monkeypatch.setattr(cli_mod, "_write_outputs", lambda *args, **kwargs: None)

    async def fake_run_suite(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cli_mod, "run_suite", fake_run_suite)

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"suite_sha": "base-sha"}), encoding="utf-8")
    args = Namespace(
        suite="suite.jsonl",
        out=str(tmp_path / "out"),
        judge=False,
        judge_scope="all",
        profile=None,
        limit=None,
        baseline=str(baseline),
        db=None,
        allow_warn=False,
    )
    assert cli_mod.cmd_run(args) == 2
