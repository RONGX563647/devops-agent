"""门禁脚本单元测试：阈值读取与变异得分解析。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load_script() -> ModuleType:
    path = ROOT / "scripts" / "check_gates.py"
    spec = importlib.util.spec_from_file_location("check_gates", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_gates"] = module
    spec.loader.exec_module(module)
    return module


check_gates = _load_script()


def test_load_gates_reads_yaml() -> None:
    gates = check_gates.load_gates()
    assert gates["coverage_fail_under"] == 80
    assert gates["mutation_score"] == 70
    assert gates["dead_code_min_confidence"] == 80


def test_parse_mutation_score() -> None:
    output = "Killed mutants (3/10)\nSurvived mutants (3/10):\n"
    assert check_gates.parse_mutation_score(output) == 70
    assert check_gates.parse_mutation_score("nothing here") is None
    assert check_gates.parse_mutation_score("Survived mutants (0/0)") is None


def test_report_returns_outcome() -> None:
    assert check_gates._report("x", True, "detail") is True
    assert check_gates._report("x", False, "detail") is False
