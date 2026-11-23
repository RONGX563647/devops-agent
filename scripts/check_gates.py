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
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
GATES_FILE = ROOT / "config" / "gates.yaml"


def load_gates() -> dict[str, int]:
    """读取 gates.yaml，返回扁平化的阈值表。"""
    with GATES_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw = data.get("quality_gates", data)
    return {str(key): int(value) for key, value in raw.items()}


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """执行命令并回显，返回完整结果。"""
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def _report(name: str, ok: bool, detail: str) -> bool:
    print(f"[{name}] {'PASS' if ok else 'FAIL'} — {detail}", flush=True)
    return ok


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
        ]
    )
    if proc.returncode != 0:
        print(proc.stdout[-4000:], flush=True)
        print(proc.stderr[-2000:], flush=True)
        return _report("coverage", False, f"pytest 退出码 {proc.returncode}")
    if not xml_path.exists():
        return _report("coverage", False, "coverage.xml 未生成")
    rate = float(ET.parse(xml_path).getroot().attrib["line-rate"]) * 100
    return _report("coverage", rate >= threshold, f"{rate:.1f}% >= {threshold}%")


def check_dead_code(confidence: int) -> bool:
    """用 yaml 里的置信度跑 vulture。"""
    proc = run([sys.executable, "-m", "vulture", str(SRC), "--min-confidence", str(confidence)])
    if proc.stdout.strip():
        print(proc.stdout.strip(), flush=True)
    return _report("dead_code", proc.returncode == 0, f"min-confidence={confidence}")


def check_complexity(threshold: int) -> bool:
    """解析 radon JSON，断言最大圈复杂度不超过阈值。"""
    proc = run([sys.executable, "-m", "radon", "cc", str(SRC), "-j"])
    if proc.returncode != 0:
        print(proc.stderr[-2000:], flush=True)
        return _report("complexity", False, "radon 执行失败")
    blocks = [
        (int(block["complexity"]), file, str(block["name"]))
        for file, items in json.loads(proc.stdout).items()
        for block in items
    ]
    worst = max(blocks, default=(0, "-", "-"))
    return _report(
        "complexity",
        worst[0] <= threshold,
        f"max CC {worst[0]} @ {worst[1]}:{worst[2]} <= {threshold}",
    )


def parse_mutation_score(output: str) -> int | None:
    """从 mutmut results 输出解析变异得分。"""
    match = re.search(r"Survived mutants \((\d+)/(\d+)\)", output)
    if match is None:
        return None
    survived, total = int(match.group(1)), int(match.group(2))
    if total == 0:
        return None
    return round((total - survived) / total * 100)


def check_mutation(threshold: int) -> bool:
    """跑 mutmut 并用 yaml 阈值判定变异得分（慢，默认跳过）。"""
    run_proc = run([sys.executable, "-m", "mutmut", "run", "--paths-to-mutate", "src"])
    if run_proc.returncode != 0:
        print(run_proc.stdout[-2000:], flush=True)
        return _report("mutation", False, "mutmut run 失败")
    result = run([sys.executable, "-m", "mutmut", "results"])
    score = parse_mutation_score(result.stdout)
    if score is None:
        return _report("mutation", False, "无法解析 mutmut results")
    return _report("mutation", score >= threshold, f"{score}% >= {threshold}%")


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

    results = [checks[name]() for name in selected]
    print(
        f"[duplicate] SKIP — 阈值 {gates['duplicate_code_threshold']}% 已读取；"
        "未配置重复代码检测器（radon/mutmut/vulture 均不覆盖此项）",
        flush=True,
    )
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
