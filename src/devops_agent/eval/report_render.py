"""把聚合 / 对比报告渲染成人读的 Markdown。

从 `report.py` 拆出来：渲染只依赖 summary dict 的结构，与聚合口径完全解耦，
拆开后两边都能各自守住行数与圈复杂度门禁。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from devops_agent.eval.judge import JUDGE_DIMENSIONS
from devops_agent.eval.kinds import HARD_KINDS


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _render_compare(metrics: list[Any]) -> list[str]:
    """基线对比表。"""
    lines = [
        "## 基线对比",
        "",
        "| 指标 | 基线 | 当前 | Δ | 更好 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in metrics:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| {item.get('key')} | {_fmt(item.get('base'))} | {_fmt(item.get('head'))} "
            f"| {_fmt(item.get('delta'))} | {_fmt(item.get('better'))} |"
        )
    lines.append("")
    return lines


def _render_hard(hard: dict[str, Any]) -> list[str]:
    """硬失败分类表：阻断类与计分类分开，全部 7 类都出现。

    只渲染 3 类会让「14/15 通过、结论 fail」的报告看起来像「零硬失败」。
    """
    lines = [
        "## 硬失败",
        "",
        "| 分类 | 严重度 | 次数 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for kind in HARD_KINDS:
        severity = "阻断" if kind.severity == "blocking" else "计分"
        lines.append(
            f"| {kind.label}（{kind.key}） | {severity} | {hard.get(kind.key, 0)} | {kind.hint} |"
        )
    lines.append("")
    return lines


def _render_judge(judge: dict[str, Any]) -> list[str]:
    """Judge 各维度均值表。"""
    lines = ["## Judge 均值", "", "| 维度 | 分数 |", "| --- | --- |"]
    for dimension in JUDGE_DIMENSIONS:
        if dimension in judge:
            lines.append(f"| {dimension} | {_fmt(judge[dimension])} |")
    lines.append("")
    return lines


def _render_tags(tags: dict[str, Any]) -> list[str]:
    """标签分布表。"""
    lines = ["## 标签分布", "", "| 标签 | 用例 | 通过 |", "| --- | --- | --- |"]
    for tag, bucket in sorted(tags.items()):
        if isinstance(bucket, dict):
            lines.append(f"| {tag} | {bucket.get('n', 0)} | {bucket.get('passed', 0)} |")
    lines.append("")
    return lines


def _render_subjects(subjects: dict[str, Any]) -> list[str]:
    """按 subject 归因：这次分数变化能归到哪个被测因子。

    这套分组此前算出来却从不渲染，「capability 类用例不算提示词因果」
    这个设计意图在报告里完全看不见。
    """
    lines = [
        "## 被测因子分布",
        "",
        "| 被测因子 | 用例 | 通过 | 通过率 |",
        "| --- | --- | --- | --- |",
    ]
    for subject, bucket in sorted(subjects.items()):
        if not isinstance(bucket, dict):
            continue
        n = bucket.get("n", 0)
        passed = bucket.get("passed", 0)
        rate = f"{passed / n:.1%}" if n else "-"
        lines.append(f"| {subject} | {n} | {passed} | {rate} |")
    lines.append("")
    return lines


_GATE_REASON_TEXT: dict[str, str] = {
    "n_below_gate": "题库样本不足",
    "no_samples": "裁判未运行",
    "samples_below_min": "裁判样本不足",
}


def _render_gates(gates: list[Any]) -> list[str]:
    """门禁三态表：通过的、未达的、样本不足的分开看。"""
    state_text = {"pass": "通过", "fail": "未达", "insufficient": "样本不足"}
    lines = [
        "## 门禁",
        "",
        "| 指标 | 值 | 阈值 | 样本 | 结论 | 原因 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        reason = _GATE_REASON_TEXT.get(str(gate.get("reason") or ""), "-")
        lines.append(
            f"| {gate.get('key')} | {_fmt(gate.get('value'))} | {_fmt(gate.get('min'))} "
            f"| {_fmt(gate.get('samples'))} | {state_text.get(str(gate.get('state')), '-')} "
            f"| {reason} |"
        )
    lines.append("")
    return lines


def _render_compatibility(compat: dict[str, Any]) -> list[str]:
    """可比性结论：不可比时必须显式写出来，否则 delta 会被当成结论。"""
    level = str(compat.get("level") or "")
    if level in ("", "clean"):
        return []
    label = {"blocking": "不可比", "confounded": "混杂", "unknown": "未知"}.get(level, level)
    reasons = compat.get("reasons")
    detail = "；".join(str(item) for item in reasons) if isinstance(reasons, list) else ""
    return [f"- 可比性：**{label}** —— {detail}", ""]


# 报告的节次顺序：容器类型不对就跳过（残缺 summary 也要能渲染），
# `keep_empty` 表示空容器仍渲染表头（对比表没有数据也要说明「没有变化」）。
_SECTIONS: tuple[tuple[str, type, bool, Callable[[Any], list[str]]], ...] = (
    ("compatibility", dict, True, _render_compatibility),
    ("gates", list, False, _render_gates),
    ("metrics", list, True, _render_compare),
    ("hard_failures", dict, True, _render_hard),
    ("judge", dict, False, _render_judge),
    ("subjects", dict, False, _render_subjects),
    ("tags", dict, False, _render_tags),
)


def _render_header(summary: dict[str, Any]) -> list[str]:
    lines = ["# 评测报告", "", f"- 结论：**{summary.get('verdict', '-')}**"]
    if "n" in summary:
        lines.append(
            f"- 用例：{summary.get('passed', 0)}/{summary.get('n', 0)} 通过"
            f"（pass_rate {_fmt(summary.get('pass_rate'))}）"
        )
    mde = summary.get("mde")
    if isinstance(mde, (int, float)):
        lines.append(
            f"- 精度上限：n={summary.get('n', '-')}，小于 {float(mde) * 100:.1f}pp 的变化不显著"
        )
    return [*lines, ""]


def _render_sections(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, kind, keep_empty, render in _SECTIONS:
        value = summary.get(key)
        if isinstance(value, kind) and (value or keep_empty):
            lines.extend(render(value))
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    """把聚合报告（或对比报告）渲染成可提交到 PR 的 Markdown。"""
    lines = _render_header(summary)
    lines.extend(_render_sections(summary))
    rate = summary.get("cache_hit_rate")
    cache_text = (
        f"；前缀缓存命中 {rate * 100:.1f}%"
        f"（命中 {_fmt(summary.get('cache_input'))}"
        f" / 写入 {_fmt(summary.get('cache_creation'))}）"
        if isinstance(rate, (int, float))
        else "；前缀缓存：无数据"
    )
    lines.append(
        f"- 轨迹分：{_fmt(summary.get('trajectory_score'))}；"
        f"p95：{_fmt(summary.get('p95_ms'))} ms；"
        f"tokens：{_fmt(summary.get('tokens_total'))}"
        f"{cache_text}"
    )
    lines.extend(_render_quality_breakdown(summary))
    return "\n".join(lines).rstrip() + "\n"


def _render_quality_breakdown(summary: dict[str, Any]) -> list[str]:
    """风险分层、漏斗与归因：报告里必须能一眼看出「为什么没过」。"""
    lines: list[str] = []
    risk = summary.get("risk")
    if isinstance(risk, dict) and risk:
        lines.extend(
            [
                "",
                "### 风险分层",
                "",
                "| 分层 | n | 有效样本 | 有效通过率（门禁） | 能力通过率 | 红线失败 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for layer, bucket in risk.items():
            lines.append(
                f"| {layer} | {bucket['n']} | {bucket.get('attributed', 0)} | "
                f"{float(bucket.get('attributed_rate', 0.0)):.1%} | "
                f"{float(bucket['capability_rate']):.1%} | {bucket['safety_failures']} |"
            )
    funnel = summary.get("funnel")
    if isinstance(funnel, list) and funnel:
        chain = " → ".join(f"{row['stage']} {row['n']}" for row in funnel)
        lines.extend(["", "### 判定漏斗", "", chain])
    attribution = summary.get("attribution")
    if isinstance(attribution, dict) and attribution:
        lines.extend(["", "### 归因", "", "| 归因 | 用例数 | 其中能力通过 |"])
        lines.append("| --- | --- | --- |")
        for key, bucket in attribution.items():
            lines.append(f"| {key} | {bucket['n']} | {bucket['passed']} |")
    return lines
