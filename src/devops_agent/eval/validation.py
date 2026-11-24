"""题库语义校验 —— id / 配比 / 断言 / 门禁阈值 / 风险分层的静态一致性检查。

拆自 `dataset.py`：那里只定义 schema 与加载，校验规则单独成模块，
新增规则时不必再挤进已经接近行数上限的 dataset。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from devops_agent.eval.dataset import (
    ALLOWED_JUDGE_DIMENSIONS,
    GRADER_VERSION,
    RATIO_TARGETS,
    RATIO_TOLERANCE,
    RISK_LAYERS,
    RISK_TARGETS,
    SCOPES,
    SUBJECTS,
    EvalCase,
    Expected,
    Manifest,
    Suite,
    ThresholdSpec,
    scenario_of,
    suite_sha256,
)


def _has_assertion(expected: Expected) -> bool:
    """是否至少有一条可判定的断言（非默认状态也算一条）。"""
    return bool(
        expected.status != "success"
        or expected.must_contain
        or expected.must_not_contain
        or expected.must_contain_any
        or expected.must_not_regex
        or expected.must_cite
        or expected.regex
        or expected.tool_calls is not None
        or expected.file_state
    )


def _case_tag_errors(case: EvalCase) -> list[str]:
    """标签必须有值、非空且带 scenario:*。"""
    if not case.tags:
        return [f"{case.id}: tags 不能为空"]
    if any(not tag.strip() for tag in case.tags):
        return [f"{case.id}: tags 不能包含空字符串"]
    if scenario_of(case) is None:
        return [f"{case.id}: 缺少 scenario:* 标签"]
    return []


def _case_errors(case: EvalCase) -> list[str]:
    """单个用例的标签、断言与 judge 维度校验。"""
    errors = _case_tag_errors(case)
    if not _has_assertion(case.expected):
        errors.append(f"{case.id}: expected 至少需要一条断言")
    if case.subject not in SUBJECTS:
        errors.append(f"{case.id}: 未知 subject: {case.subject}（可选 {', '.join(SUBJECTS)}）")
    if case.judge is not None:
        unknown = sorted(set(case.judge.dimensions) - ALLOWED_JUDGE_DIMENSIONS)
        if unknown:
            errors.append(f"{case.id}: 未知 judge 维度: {', '.join(unknown)}")
    if case.risk is not None and case.risk not in RISK_LAYERS:
        errors.append(f"{case.id}: 未知 risk: {case.risk}（可选 {', '.join(RISK_LAYERS)}）")
    unknown_scopes = sorted(set(case.scopes) - set(SCOPES))
    if unknown_scopes:
        errors.append(f"{case.id}: 未知 scope: {', '.join(unknown_scopes)}")
    errors.extend(_assertion_errors(case))
    return errors


def _cite_errors(case_id: str, paths: list[str]) -> list[str]:
    """`must_cite` 只接受工作区内的相对路径（绝对路径与 `..` 会指向评测机本身）。"""
    errors: list[str] = []
    for path in paths:
        if not path.strip():
            errors.append(f"{case_id}: must_cite 含空路径")
        elif path.startswith(("/", "\\")) or ".." in Path(path).parts:
            errors.append(f"{case_id}: must_cite 只接受工作区内的相对路径: {path}")
    return errors


def _assertion_errors(case: EvalCase) -> list[str]:
    """断言自身的合法性：路径不许越界、正则必须可编译、语义组不许为空。"""
    expected = case.expected
    errors = _cite_errors(case.id, expected.must_cite)
    for index, group in enumerate(expected.must_contain_any):
        if not [word for word in group if word.strip()]:
            errors.append(f"{case.id}: must_contain_any[{index}] 为空组")
    for pattern in [*expected.regex, *expected.must_not_regex]:
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"{case.id}: 非法正则 {pattern}: {exc}")
    return errors


def _counts_errors(suite: Suite, actual_counts: Counter[str]) -> list[str]:
    """manifest 声明的场景计数必须与实际一致。"""
    if dict(actual_counts) == dict(suite.manifest.counts):
        return []
    return [
        f"manifest.counts 与实际不一致: 期望 {suite.manifest.counts}, 实际 {dict(actual_counts)}"
    ]


def _ratio_errors(n: int, actual_counts: Counter[str]) -> list[str]:
    """场景配比必须落在目标区间（含容差）内。"""
    errors: list[str] = []
    for scenario, (low, high) in RATIO_TARGETS.items():
        share = actual_counts.get(scenario, 0) / n
        if not (low - RATIO_TOLERANCE <= share <= high + RATIO_TOLERANCE):
            errors.append(
                f"scenario:{scenario} 占比 {share:.1%} 超出 "
                f"[{low:.0%}, {high:.0%}] ±{RATIO_TOLERANCE:.0%}"
            )
    return errors


GATE_KEYS: tuple[str, ...] = ("pass_rate", "judge_correctness", "trajectory")
# 每个门禁的合法取值区间；用来拦住「pass_rate: 80」这种把百分比当小数的写法。
_GATE_RANGES: dict[str, tuple[float, float]] = {
    "pass_rate": (0.0, 1.0),
    "judge_correctness": (1.0, 5.0),
    "trajectory": (0.0, 1.0),
}


def _threshold_errors(thresholds: dict[str, ThresholdSpec]) -> list[str]:
    """阈值必须是已知门禁、取值在量纲内、min_samples 非负。"""
    errors: list[str] = []
    for key, spec in thresholds.items():
        if key not in GATE_KEYS:
            errors.append(f"warning: 未知门禁 {key}，报告不会消费它")
            continue
        low, high = _GATE_RANGES[key]
        if not (low <= spec.min <= high):
            errors.append(f"thresholds.{key}.min {spec.min} 超出量纲 [{low}, {high}]")
        if spec.min_samples < 0:
            errors.append(f"thresholds.{key}.min_samples 不能为负")
    return errors


def _risk_errors(suite: Suite, n: int, actual: Counter[str]) -> list[str]:
    """风险分层的计数、配比与阈值校验。

    只有在 manifest 声明了 `risk_counts` 时才启用：v2 题库没有 risk 字段，
    不能因为新增校验就把老题库判成非法。
    """
    manifest = suite.manifest
    if not manifest.risk_counts:
        return []
    return [
        *_risk_count_errors(suite, n, actual),
        *_risk_layer_errors(manifest.risk_thresholds, manifest.min_cases_per_risk),
    ]


def _risk_count_errors(suite: Suite, n: int, actual: Counter[str]) -> list[str]:
    """用例缺分层、计数不一致、配比越界。"""
    manifest = suite.manifest
    errors: list[str] = []
    missing = [case.id for case in suite.cases if case.risk is None]
    if missing:
        preview = ", ".join(missing[:5])
        errors.append(f"{len(missing)} 条用例缺少 risk 分层: {preview}")
    if dict(actual) != dict(manifest.risk_counts):
        errors.append(
            f"manifest.risk_counts 与实际不一致: 期望 {manifest.risk_counts}, 实际 {dict(actual)}"
        )
    if n < manifest.min_cases_for_gate:
        return errors
    for layer, (low, high) in RISK_TARGETS.items():
        share = actual.get(layer, 0) / n
        if not (low - RATIO_TOLERANCE <= share <= high + RATIO_TOLERANCE):
            errors.append(
                f"risk:{layer} 占比 {share:.1%} 超出 [{low:.0%}, {high:.0%}] ±{RATIO_TOLERANCE:.0%}"
            )
    return errors


def _risk_layer_errors(thresholds: dict[str, float], min_cases: dict[str, int]) -> list[str]:
    """分层阈值/最小样本里的未知分层与越界值。"""
    errors: list[str] = []
    for layer, value in thresholds.items():
        if layer not in RISK_LAYERS:
            errors.append(f"warning: risk_thresholds 含未知分层 {layer}，报告不会消费它")
        elif not 0.0 <= value <= 1.0:
            errors.append(f"risk_thresholds.{layer} {value} 超出量纲 [0, 1]")
    for layer, value in min_cases.items():
        if layer not in RISK_LAYERS:
            errors.append(f"min_cases_per_risk 含未知分层 {layer}")
        elif value < 0:
            errors.append(f"min_cases_per_risk.{layer} 不能为负")
    return errors


def validate_suite(suite: Suite) -> list[str]:
    """返回语义校验错误；`warning:` 开头的条目只提示、不阻断。

    少于 `min_cases_for_gate` 时只给 warning，且跳过配比硬校验 ——
    小样本下百分比抖动没有意义，强行要求配比只会逼人凑数。
    """
    errors = _identity_errors(suite)
    actual_counts = Counter(
        scenario for case in suite.cases if (scenario := scenario_of(case)) is not None
    )
    errors.extend(_counts_errors(suite, actual_counts))

    n = len(suite.cases)
    risk_actual = Counter(case.risk for case in suite.cases if case.risk is not None)
    errors.extend(_risk_errors(suite, n, risk_actual))
    # 顺序保持：既有 warning 的先后次序（样本量、配比）不变，
    # 依赖 `warnings[0]` 的调用方不会被「清单元数据」类提示挤掉。
    errors.extend(_size_errors(suite, n, actual_counts))
    errors.extend(_grader_errors(suite.manifest))
    errors.extend(_threshold_errors(suite.manifest.thresholds))
    return errors


def _identity_errors(suite: Suite) -> list[str]:
    """重复 id、逐用例错误、sha 不匹配。"""
    errors: list[str] = []
    seen: set[str] = set()
    for case in suite.cases:
        if case.id in seen:
            errors.append(f"duplicate case id: {case.id}")
        seen.add(case.id)
        errors.extend(_case_errors(case))
    actual_sha = suite_sha256(suite.cases)
    if suite.manifest.sha256 != actual_sha:
        errors.append(f"manifest.sha256 不匹配: 期望 {suite.manifest.sha256}, 实际 {actual_sha}")
    return errors


def _size_errors(suite: Suite, n: int, actual_counts: Counter[str]) -> list[str]:
    """样本量与配比：未达门禁量只警告，达到后按题库形态校验配比。"""
    if n < suite.manifest.min_cases_for_gate:
        return [
            f"warning: 用例数 {n} < min_cases_for_gate "
            f"{suite.manifest.min_cases_for_gate}，不参与发布门禁"
        ]
    if not suite.manifest.risk_counts:
        # v3 用风险分层配比；v2 继续用 scenario 配比，二者不叠加。
        return _ratio_errors(n, actual_counts)
    return []


def _grader_errors(manifest: Manifest) -> list[str]:
    """判卷口径必须可追溯：缺失或漂移都提示。"""
    declared = manifest.grader_version
    if not declared:
        return ["warning: manifest 未记录 grader_version，判卷口径变更无法追溯"]
    if declared != GRADER_VERSION:
        return [
            f"manifest.grader_version {declared} != 当前判卷口径 {GRADER_VERSION}，"
            "新旧批次分数不可直接比较"
        ]
    return []


def split_warnings(messages: list[str]) -> tuple[list[str], list[str]]:
    """把校验输出拆成 (errors, warnings)，CLI 据此决定退出码。"""
    errors = [item for item in messages if not item.startswith("warning:")]
    warnings = [item for item in messages if item.startswith("warning:")]
    return errors, warnings
