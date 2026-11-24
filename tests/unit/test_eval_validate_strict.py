"""`validate --strict` 的语义单测。

锁一个容易被忽略的状态：题库没到 `min_cases_for_gate` 时 `validate` 只给 warning，
退出码 0。于是题库可以安静地停在 39 条 —— 评测一直在跑，但门禁永远 `insufficient`，
从不给结论。

`--strict` 把 warning 也当阻断，让这种「差一条」在 CI 里显形。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.eval.cli import main


def _write_suite(root: Path, case_count: int) -> Path:
    """生成一个 case_count 条的合法题库（含 manifest）。"""
    cases = []
    for index in range(case_count):
        cases.append(
            {
                "id": f"c-{index:03d}",
                "tags": ["scenario:common"],
                "input": "x",
                "permission": "readonly",
                "expected": {"status": "success", "must_contain": ["ok"]},
                "subject": "prompt",
            }
        )
    path = root / "demo.v1.jsonl"
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    # 用正式 API 算 sha 与清单，避免手写错
    from devops_agent.eval.dataset import EvalCase, Manifest, suite_sha256

    parsed = [EvalCase.model_validate(case) for case in cases]
    manifest = Manifest(
        suite="demo",
        version="1.0.0",
        sha256=suite_sha256(parsed),
        counts={"common": case_count},
        min_cases_for_gate=40,
        thresholds={
            "pass_rate": {"min": 0.8, "min_samples": 0},
            "judge_correctness": {"min": 4.0, "min_samples": 10},
            "trajectory": {"min": 0.85, "min_samples": 10},
        },
        grader_version="v2",
    )
    path.with_name("demo.v1.manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def test_below_gate_warns_but_passes_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认模式下，39 条题库只是 warning —— 这正是问题所在。"""
    suite = _write_suite(tmp_path, 39)
    code = main(["validate", "--suite", str(suite)])
    captured = capsys.readouterr()
    assert code == 0
    assert "warning" in captured.out
    assert "ok 39 cases" in captured.out


def test_below_gate_fails_with_strict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--strict` 下同样的题库必须阻断。"""
    suite = _write_suite(tmp_path, 39)
    code = main(["validate", "--suite", str(suite), "--strict"])
    captured = capsys.readouterr()
    assert code == 1
    assert "视为阻断" in captured.err


def test_strict_passes_when_suite_is_healthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """达到门禁量、配比正常、无 warning 时，`--strict` 应通过。"""
    # 40 条全是 common 会触发配比 warning，所以这里只断言「无 warning 才通过」这一点：
    # 构造成 40 条并接受配比 warning 之外的行为差异，用真实题库验证更可靠
    suite = Path("eval/suites/devops-core.v2.jsonl")
    code = main(["validate", "--suite", str(suite), "--strict"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "ok 40 cases" in captured.out


def test_real_v1_suite_fails_strict() -> None:
    """v1 题库（15 条）在 `--strict` 下必须被拦住。"""
    legacy = Path("eval/suites/devops-core.v1.jsonl")
    if not legacy.exists():
        pytest.skip("v1 题库已移除")
    assert main(["validate", "--suite", str(legacy), "--strict"]) == 1
    # 不带 --strict 时仍通过（历史行为不变）
    assert main(["validate", "--suite", str(legacy)]) == 0


def test_ci_uses_strict() -> None:
    """CI 必须带 --strict，否则题库停在门禁量之下也没人发现。"""
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    validate_lines = [line for line in ci.splitlines() if "eval validate" in line]
    assert validate_lines, "CI 里找不到 validate 步骤"
    assert all("--strict" in line for line in validate_lines), validate_lines
