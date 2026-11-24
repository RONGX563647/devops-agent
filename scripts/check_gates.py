#!/usr/bin/env python3
"""质量门禁执行器 —— 阈值全部来自 config/gates.yaml。

用法：
    python scripts/check_gates.py                    # complexity / dead_code / coverage
    python scripts/check_gates.py --only coverage    # 单项
    python scripts/check_gates.py --with-mutation    # 追加变异测试（慢）
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 子进程超时：工具挂死时宁可让门禁失败，也不要把 CI 拖到被平台强杀 ——
# 强杀只有一句「job 超时」，看不出是哪个工具卡住。静态工具秒级完成，120s 足够；
# 覆盖率的 pytest 与 mutmut 都要跑多轮测试，单独放宽到 300s。
_TOOL_TIMEOUT_SECONDS = 120
_SLOW_TIMEOUT_SECONDS = 300


class _GateTimeout(RuntimeError):
    """门禁命令超时。单独类型，便于 main 区分「卡死」与「不达标」。"""

    def __init__(self, label: str, timeout: int) -> None:
        super().__init__(f"{label} 超过 {timeout}s 未返回")
        self.label = label
        self.timeout = timeout


def load_gates() -> dict[str, int]:
    """读取 gates.yaml，复用应用配置层的强类型加载器（阈值单一来源）。

    显式把当前仓库的 src 放到 sys.path 最前，确保加载的是本次 checkout 的配置层，
    而不是 editable install .pth 指向的其它副本；函数内 import 避免模块级副作用。
    """
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from devops_agent.config import load_gates as load_app_gates

    gates = load_app_gates()
    return gates.model_dump()


def run(cmd: list[str], *, label: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """执行命令并回显，返回完整结果；超时抛 `_GateTimeout`。"""
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # TimeoutExpired 自带的信息只有命令行，看不出是哪个门禁；转成带 label 的
        # 异常交给 main 打印，避免整段栈把真正的诊断信息淹掉。
        raise _GateTimeout(label, timeout) from None


def _report(name: str, ok: bool, detail: str) -> bool:
    print(f"[{name}] {'PASS' if ok else 'FAIL'} — {detail}", flush=True)
    return ok


def _at_least(actual: str, threshold: int, ok: bool) -> str:
    """「不低于阈值」类门禁的比较式。

    失败行必须写 `<`：写成 `62.3% >= 80%` 会让读 CI 日志的人以为门禁自相矛盾。
    """
    return f"{actual} {'>=' if ok else '<'} {threshold}"


def _at_most(actual: str, threshold: int, ok: bool) -> str:
    """「不超过阈值」类门禁（圈复杂度）的比较式，失败时写 `>`。"""
    return f"{actual} {'<=' if ok else '>'} {threshold}"


def _pct(value: float) -> str:
    """百分比显示：按 0.1 截断而非四舍五入。

    四舍五入会把 79.96% 印成 `80.0% < 80%`，读起来像门禁算错了；截断后
    显示值永远不高于真实值，与「宁严勿松」的判定方向一致。
    """
    return f"{math.floor(value * 10) / 10:.1f}%"


def check_coverage(threshold: int) -> bool:
    """跑 pytest-cov，用 yaml 阈值判定覆盖率。"""
    xml_path = ROOT / "coverage.xml"
    if xml_path.exists():
        xml_path.unlink()
    proc = run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--cov=devops_agent",
            "--cov-report=term",
            "--cov-report=xml",
            f"--cov-fail-under={threshold}",
        ],
        label="coverage",
        timeout=_SLOW_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        print(proc.stdout[-4000:], flush=True)
        print(proc.stderr[-2000:], flush=True)
        return _report("coverage", False, f"pytest 退出码 {proc.returncode}")
    if not xml_path.exists():
        return _report("coverage", False, "coverage.xml 未生成")
    rate = float(ET.parse(xml_path).getroot().attrib["line-rate"]) * 100
    ok = rate >= threshold
    return _report("coverage", ok, _at_least(_pct(rate), threshold, ok))


def check_dead_code(confidence: int) -> bool:
    """用 yaml 里的置信度跑 vulture。"""
    proc = run(
        [sys.executable, "-m", "vulture", str(SRC), "--min-confidence", str(confidence)],
        label="dead_code",
        timeout=_TOOL_TIMEOUT_SECONDS,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip(), flush=True)
    ok = proc.returncode == 0
    if not ok and proc.stderr.strip():
        # vulture 崩溃（参数错误/内部异常）与「确实扫到死代码」都是非零退出，
        # 不回显 stderr 就分不清是哪种，CI 上只能看到一行 FAIL。
        print(proc.stderr.strip(), flush=True)
    return _report("dead_code", ok, f"min-confidence={confidence}")


def check_complexity(threshold: int) -> bool:
    """解析 radon JSON，断言最大圈复杂度不超过阈值。"""
    proc = run(
        [sys.executable, "-m", "radon", "cc", str(SRC), "-j"],
        label="complexity",
        timeout=_TOOL_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        print(proc.stderr[-2000:], flush=True)
        return _report("complexity", False, "radon 执行失败")
    blocks = [
        (int(block["complexity"]), file, str(block["name"]))
        for file, items in json.loads(proc.stdout).items()
        for block in items
    ]
    worst = max(blocks, default=(0, "-", "-"))
    ok = worst[0] <= threshold
    return _report(
        "complexity",
        ok,
        f"max CC {_at_most(str(worst[0]), threshold, ok)} @ {worst[1]}:{worst[2]}",
    )


def parse_mutation_score(output: str) -> int | None:
    """从 mutmut results 输出解析变异得分（向下取整）。"""
    match = re.search(r"Survived mutants \((\d+)/(\d+)\)", output)
    if match is None:
        return None
    survived, total = int(match.group(1)), int(match.group(2))
    if total == 0:
        return None
    # 用 floor 而不是 round：round 的银行家舍入会对 .5 取偶，70.5 → 70 尚可，
    # 但 71.5 → 72 会让本该卡住的分数通过。门禁宁严勿松，只允许向下取整。
    return math.floor((total - survived) / total * 100)


def check_mutation(threshold: int) -> bool:
    """跑 mutmut 并用 yaml 阈值判定变异得分（慢，默认跳过）。"""
    run_proc = run(
        [sys.executable, "-m", "mutmut", "run", "--paths-to-mutate", "src"],
        label="mutation",
        timeout=_SLOW_TIMEOUT_SECONDS,
    )
    if run_proc.returncode != 0:
        print(run_proc.stdout[-2000:], flush=True)
        return _report("mutation", False, "mutmut run 失败")
    result = run(
        [sys.executable, "-m", "mutmut", "results"],
        label="mutation",
        timeout=_TOOL_TIMEOUT_SECONDS,
    )
    score = parse_mutation_score(result.stdout)
    if score is None:
        return _report("mutation", False, "无法解析 mutmut results")
    ok = score >= threshold
    return _report("mutation", ok, _at_least(f"{score}%", threshold, ok))


def main() -> int:
    parser = argparse.ArgumentParser(description="从 config/gates.yaml 执行质量门禁")
    parser.add_argument(
        "--only",
        choices=["coverage", "dead_code", "complexity", "mutation"],
        help="只跑指定门禁",
    )
    parser.add_argument("--with-mutation", action="store_true", help="追加变异测试（慢）")
    args = parser.parse_args()

    gates = load_gates()
    print("Quality gates from config/gates.yaml:", flush=True)
    for name, value in gates.items():
        print(f"  {name}: {value}", flush=True)

    checks: dict[str, Callable[[], bool]] = {
        "complexity": lambda: check_complexity(gates["complexity_threshold"]),
        "dead_code": lambda: check_dead_code(gates["dead_code_min_confidence"]),
        "coverage": lambda: check_coverage(gates["coverage_fail_under"]),
        "mutation": lambda: check_mutation(gates["mutation_score"]),
    }

    selected = [args.only] if args.only else ["complexity", "dead_code", "coverage"]
    if args.with_mutation and "mutation" not in selected:
        selected.append("mutation")

    results: list[bool] = []
    for name in selected:
        try:
            results.append(checks[name]())
        except _GateTimeout as exc:
            # 超时走与「不达标」相同的失败路径：打印是哪个门禁卡住并返回 1，
            # 不向上抛栈 —— CI 日志里一行诊断比 traceback 更好定位。
            results.append(_report(exc.label, False, f"超过 {exc.timeout}s 未返回，已中止"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
