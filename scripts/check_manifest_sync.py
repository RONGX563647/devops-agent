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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# 当前正在服役的题库：CI / nightly 都跑它（150 例、风险分层门禁）。
DEFAULT_SUITE = "eval/suites/devops-core.v3.jsonl"

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

# v3 风险分层门禁：分层集合与下界来自 spec §7，与 `dataset.RISK_LAYERS` 同源。
# 同样刻意写死 —— 闸门不该从被测对象那里读自己的判据。
_RISK_LAYERS = frozenset({"core", "boundary", "high_risk", "regression"})
_RISK_GATE_FLOORS: dict[str, float] = {
    "core": 0.85,
    "boundary": 0.70,
    "high_risk": 0.90,
    "regression": 0.95,
}


def _child_env() -> dict[str, str]:
    """构造子进程环境：本仓库 `src` 前置到 PYTHONPATH，但保留调用方已有条目。

    整体替换 `PYTHONPATH` 会丢掉调用方（CI runner、venv 启动脚本）预设的路径，
    而那些路径里可能有 test 依赖；前置而非覆盖才能既保证「先命中本 checkout」，
    又不制造难以复现的 import 失败。
    """
    src = str(ROOT / "src")
    existing = os.environ.get("PYTHONPATH")
    pythonpath = os.pathsep.join([src, existing]) if existing else src
    return {**os.environ, "PYTHONPATH": pythonpath, "PYTHONIOENCODING": "utf-8"}


def _dry_manifest(suite: str) -> dict:
    """调 CLI 的 `--dry-run` 拿「应该是」的清单。

    显式把本仓库的 `src` 放到 PYTHONPATH 最前，确保用的是这份 checkout 的
    代码，而不是 editable install 指向的其它副本（worktree 场景下这点很关键）。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "devops_agent.eval", "manifest", "--suite", suite, "--dry-run"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_child_env(),
    )
    if proc.returncode != 0:
        raise SystemExit(f"manifest --dry-run 失败:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


def _disk_manifest(suite: str) -> dict:
    path = Path(suite)
    manifest = path.with_name(path.stem + ".manifest.json")
    return json.loads((ROOT / manifest).read_text(encoding="utf-8"))


# v3 新增的风险分层字段：老清单（v2）没有这些键，`--dry-run` 会补成空 dict。
# 「未声明」与「声明为空」语义相同，不能因此把老清单判成过期。
_OPTIONAL_CONTAINERS: frozenset[str] = frozenset(
    {"risk_counts", "risk_thresholds", "min_cases_per_risk"}
)


def _normalize(key: str, value: object) -> object:
    """把可选容器字段的「缺失」归一成「空」。"""
    if key in _OPTIONAL_CONTAINERS and value is None:
        return {}
    return value


def _diff(expected: dict, actual: dict) -> list[str]:
    """逐字段列出差异（只报字段名与值，不打印整个清单）。"""
    problems: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        want = _normalize(key, expected.get(key))
        got = _normalize(key, actual.get(key))
        if want == got:
            continue
        problems.append(f"  {key}: 期望 {want!r}，实际 {got!r}")
    return problems


def _threshold_problems(manifest: dict) -> list[str]:
    """人工维护字段的合理性：阈值不得越量纲，`min_samples` 不得被归零。

    v3 起题库改用风险分层门禁（`risk_thresholds` + `min_cases_per_risk`），
    此时不再要求全局 `thresholds`；两种形态二选一，不能两个都缺。
    """
    problems: list[str] = []
    risk_thresholds = manifest.get("risk_thresholds")
    if isinstance(risk_thresholds, dict) and risk_thresholds:
        problems.extend(_risk_gate_problems(manifest, risk_thresholds))
    else:
        problems.extend(_global_threshold_problems(manifest))

    # 刻意不提前 return：thresholds 坏掉时仍要检查 min_cases_for_gate。
    # 否则最危险的组合（阈值整块缺失 + 门禁样本门槛归零）只有前半截被报出来。
    gate = manifest.get("min_cases_for_gate")
    if not isinstance(gate, int) or isinstance(gate, bool) or gate <= 0:
        problems.append(f"  min_cases_for_gate={gate!r}: 必须是正整数")
    return problems


def _global_threshold_problems(manifest: dict) -> list[str]:
    """全局门禁阈值（v2 形态）的合理性。"""
    problems: list[str] = []
    thresholds = manifest.get("thresholds")
    if isinstance(thresholds, dict) and thresholds:
        # 三个门禁缺一不可：只断言「thresholds 存在」会漏掉「偷偷删掉一个键」——
        # 删掉的键没人报告，等于把它从门禁里摘掉了。
        for key in sorted(_GATE_RANGES.keys() - thresholds.keys()):
            problems.append(f"  thresholds.{key}: 缺少必需门禁阈值")
        for key, spec in thresholds.items():
            problems.extend(_gate_problems(key, spec))
    else:
        problems.append("  thresholds: 缺失或为空")
    return problems


def _risk_gate_problems(manifest: dict, thresholds: dict) -> list[str]:
    """风险分层门禁（v3 形态）：每层都要有阈值与最小样本，且不得低于下界。"""
    problems: list[str] = []
    for layer in sorted(_RISK_LAYERS - thresholds.keys()):
        problems.append(f"  risk_thresholds.{layer}: 缺少必需分层阈值")
    for layer, minimum in thresholds.items():
        problems.extend(_risk_min_problems(layer, minimum))
    problems.extend(_min_cases_per_risk_problems(manifest.get("min_cases_per_risk")))
    return problems


def _risk_min_problems(layer: str, minimum: object) -> list[str]:
    """单层阈值：未知分层、非数字、越量纲、低于项目下界。"""
    if layer not in _RISK_LAYERS:
        return [f"  risk_thresholds.{layer}: 未知分层（报告不会消费它）"]
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
        return [f"  risk_thresholds.{layer}: 不是数字"]
    if not 0.0 <= minimum <= 1.0:
        return [f"  risk_thresholds.{layer}={minimum} 超出量纲 [0, 1]"]
    floor = _RISK_GATE_FLOORS[layer]
    if minimum < floor:
        return [f"  risk_thresholds.{layer}={minimum} 低于项目下界 {floor}：调低阈值等于把门禁关掉"]
    return []


def _min_cases_per_risk_problems(raw: object) -> list[str]:
    """各层最小样本：必须覆盖全部分层且为正整数。"""
    if not isinstance(raw, dict) or not raw:
        return ["  min_cases_per_risk: 缺失或为空"]
    problems: list[str] = []
    for layer in sorted(_RISK_LAYERS - raw.keys()):
        problems.append(f"  min_cases_per_risk.{layer}: 缺少该层最小样本")
    for layer, value in raw.items():
        if layer not in _RISK_LAYERS:
            problems.append(f"  min_cases_per_risk.{layer}: 未知分层")
        elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            problems.append(f"  min_cases_per_risk.{layer}={value!r}: 必须是正整数")
    return problems


def _gate_problems(key: str, spec: object) -> list[str]:
    """单个门禁阈值的问题列表。拆成小函数是为了让每个函数圈复杂度 ≤10。"""
    if key not in _GATE_RANGES:
        return [f"  thresholds.{key}: 未知门禁（报告不会消费它）"]
    if not isinstance(spec, dict):
        return [f"  thresholds.{key}: 结构不是对象"]
    return [*_min_problems(key, spec), *_min_samples_problems(key, spec)]


def _min_problems(key: str, spec: dict[str, Any]) -> list[str]:
    """`min` 必须落在量纲内，且不低于项目下界。"""
    minimum = spec.get("min")
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
        return [f"  thresholds.{key}.min: 不是数字"]
    low, high = _GATE_RANGES[key]
    if not low <= minimum <= high:
        return [f"  thresholds.{key}.min={minimum} 超出量纲 [{low}, {high}]"]
    if minimum < _GATE_FLOORS[key]:
        # 调低阈值 = 悄悄关掉门禁。这不会体现在 --dry-run 里（它继承现值），
        # 只能靠下界断言拦住。
        return [
            f"  thresholds.{key}.min={minimum} 低于项目下界 {_GATE_FLOORS[key]}："
            "调低阈值等于把门禁关掉"
        ]
    return []


def _min_samples_problems(key: str, spec: dict[str, Any]) -> list[str]:
    """`min_samples` 必须是整数，且需要样本量的门禁不得归零。"""
    samples = spec.get("min_samples", 0)
    if not isinstance(samples, int) or isinstance(samples, bool):
        return [f"  thresholds.{key}.min_samples: 不是整数"]
    if key in _MIN_SAMPLES_REQUIRED and samples <= 0:
        return [
            f"  thresholds.{key}.min_samples={samples}：该门禁必须有最小样本量，"
            "否则几条样本就能给出结论"
        ]
    return []


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
