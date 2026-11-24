"""门禁脚本单元测试：阈值读取、变异得分解析、失败文案与超时处理。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def test_mutation_score_rounds_down() -> None:
    """139/200 = 69.5：round 的银行家舍入会给出 70，门禁宁严勿松必须取 69。"""
    assert check_gates.parse_mutation_score("Survived mutants (61/200)") == 69


def test_threshold_wording_flips_on_failure() -> None:
    """失败行不能印「62.3% >= 80%」这种自相矛盾的满足式。"""
    assert check_gates._at_least("62.3%", 80, False) == "62.3% < 80"
    assert check_gates._at_least("85.0%", 80, True) == "85.0% >= 80"
    assert check_gates._at_most("12", 10, False) == "12 > 10"
    assert check_gates._at_most("8", 10, True) == "8 <= 10"


def test_pct_truncates_instead_of_rounding() -> None:
    """79.96% 不能显示成 80.0%，否则日志看着像门禁算错了。"""
    assert check_gates._pct(79.96) == "79.9%"
    assert check_gates._pct(80.0) == "80.0%"


def test_run_times_out_with_gate_label() -> None:
    """超时要转成带门禁名的异常，而不是抛出没有上下文的 TimeoutExpired。"""
    with pytest.raises(check_gates._GateTimeout) as excinfo:
        check_gates.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            label="slow",
            timeout=1,
        )
    assert excinfo.value.label == "slow"


def test_main_reports_timeout_as_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """超时走失败路径：打印哪个门禁卡住并返回 1，不能向上抛栈。"""

    def boom(_: int) -> bool:
        raise check_gates._GateTimeout("complexity", 120)

    monkeypatch.setattr(check_gates, "check_complexity", boom)
    monkeypatch.setattr(sys, "argv", ["check_gates.py", "--only", "complexity"])
    assert check_gates.main() == 1
    assert "超过 120s 未返回" in capsys.readouterr().out


def test_dead_code_echoes_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """vulture 崩溃也是非零退出，必须回显 stderr 才能与「扫到死代码」区分。"""
    proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom: 配置错误")
    monkeypatch.setattr(check_gates, "run", lambda *_args, **_kwargs: proc)

    assert check_gates.check_dead_code(80) is False
    assert "boom: 配置错误" in capsys.readouterr().out


def test_report_returns_outcome() -> None:
    assert check_gates._report("x", True, "detail") is True
    assert check_gates._report("x", False, "detail") is False
