#!/usr/bin/env python3
"""清单与题库是否同步 —— CI 闸门。

`SuiteManifest.sha256` 是题库的规范化指纹，`eval compare` 靠它判断「两批是不是
同一套题」。改了 JSONL 却忘了跑 `eval manifest` 时，新旧批次的 delta 会无声失效。

两类字段要分开看：

- **可推导**（`sha256` / `counts` / `grader_version`）：由题库内容算出，
  用 `manifest --dry-run` 的输出来比对；
- **人工维护**（`thresholds` / `min_cases_for_gate` / `version`）：`--dry-run`
  只是把现有值继承下来，所以它发现不了「有人偷偷调低了阈值」。这类字段在这里
  单独做**合理性断言**：阈值必须落在量纲内，`min_samples` 不得被归零。

比对按 JSON 语义而非字节，避免 CRLF/缩进差异造成假红。

用法：
    python scripts/check_manifest_sync.py
    python scripts/check_manifest_sync.py --suite eval/suites/x.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 当前正在服役的题库：CI / nightly 都跑它。
DEFAULT_SUITE = "eval/suites/devops-core.v2.jsonl"

# 阈值量纲：与 `dataset._GATE_RANGES` 同源。写死在这里是刻意的 —— 闸门不该
# 从被测对象那里读自己的判据。
_GATE_RANGES: dict[str, tuple[float, float]] = {
    "pass_rate": (0.0, 1.0),
    "judge_correctness": (1.0, 5.0),
    "trajectory": (0.0, 1.0),
}
# 这两个门禁必须有最小样本量，否则 3 条样本就能让门禁给出结论。
_MIN_SAMPLES_REQUIRED = frozenset({"judge_correctness", "trajectory"})
# 阈值的**下界**：低于它等于把门禁关掉。数值来自项目约定（spec §2.5），
# 与 `_GATE_RANGES` 的「量纲」不同 —— 前者是「合不合法」，这里是「还算不算门禁」。
_GATE_FLOORS: dict[str, float] = {
    "pass_rate": 0.8,
    "judge_correctness": 4.0,
    "trajectory": 0.85,
}


def _dry_manifest(suite: str) -> dict:
    """调 CLI 的 `--dry-run` 拿「应该是」的清单。

    显式把本仓库的 `src` 放到 PYTHONPATH 最前，确保用的是这份 checkout 的
    代码，而不是 editable install 指向的其它副本（worktree 场景下这点很关键）。
    """
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "devops_agent.eval", "manifest", "--suite", suite, "--dry-run"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(f"manifest --dry-run 失败:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


def _disk_manifest(suite: str) -> dict:
    path = Path(suite)
    manifest = path.with_name(path.stem + ".manifest.json")
    return json.loads((ROOT / manifest).read_text(encoding="utf-8"))


def _diff(expected: dict, actual: dict) -> list[str]:
    """逐字段列出差异（只报字段名与值，不打印整个清单）。"""
    problems: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        want, got = expected.get(key), actual.get(key)
        if want == got:
            continue
        problems.append(f"  {key}: 期望 {want!r}，实际 {got!r}")
    return problems


def _threshold_problems(manifest: dict) -> list[str]:
    """人工维护字段的合理性：阈值不得越量纲，`min_samples` 不得被归零。"""
    problems: list[str] = []
    thresholds = manifest.get("thresholds")
    if not isinstance(thresholds, dict) or not thresholds:
        return ["  thresholds: 缺失或为空"]

    for key, spec in thresholds.items():
        if key not in _GATE_RANGES:
            problems.append(f"  thresholds.{key}: 未知门禁（报告不会消费它）")
            continue
        if not isinstance(spec, dict):
            problems.append(f"  thresholds.{key}: 结构不是对象")
            continue
        low, high = _GATE_RANGES[key]
        minimum = spec.get("min")
        if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
            problems.append(f"  thresholds.{key}.min: 不是数字")
        elif not low <= minimum <= high:
            problems.append(f"  thresholds.{key}.min={minimum} 超出量纲 [{low}, {high}]")
        elif minimum < _GATE_FLOORS[key]:
            # 调低阈值 = 悄悄关掉门禁。这不会体现在 --dry-run 里（它继承现值），
            # 只能靠下界断言拦住。
            problems.append(
                f"  thresholds.{key}.min={minimum} 低于项目下界 {_GATE_FLOORS[key]}："
                "调低阈值等于把门禁关掉"
            )
        samples = spec.get("min_samples", 0)
        if not isinstance(samples, int) or isinstance(samples, bool):
            problems.append(f"  thresholds.{key}.min_samples: 不是整数")
        elif key in _MIN_SAMPLES_REQUIRED and samples <= 0:
            problems.append(
                f"  thresholds.{key}.min_samples={samples}：该门禁必须有最小样本量，"
                "否则几条样本就能给出结论"
            )

    gate = manifest.get("min_cases_for_gate")
    if not isinstance(gate, int) or isinstance(gate, bool) or gate <= 0:
        problems.append(f"  min_cases_for_gate={gate!r}: 必须是正整数")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="校验清单与题库同步")
    parser.add_argument("--suite", default=DEFAULT_SUITE)
    args = parser.parse_args()

    expected = _dry_manifest(args.suite)
    actual = _disk_manifest(args.suite)

    problems = _diff(expected, actual)
    # 人工维护字段单独校验：--dry-run 继承现有值，发现不了它们被人改坏
    problems.extend(_threshold_problems(actual))

    if not problems:
        print(f"[manifest] OK — {args.suite} 清单与题库一致")
        return 0

    print(f"[manifest] 过期：{args.suite} 的清单与题库不一致", file=sys.stderr)
    for line in problems:
        print(line, file=sys.stderr)
    print(
        "\n修复：python -m devops_agent.eval manifest "
        f"--suite {args.suite}（阈值问题需手工改回合理值）",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
