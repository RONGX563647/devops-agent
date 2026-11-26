"""汇总与报告：结论、范围、原因、证据四件事分开说。

原来的三态（pass / fail / insufficient）把三种事实压进了一个词：
「模型好不好」（效果）、「证据齐不齐」（裁判与校准）、「测全了没有」（范围）。
三者只能取最悲观值，于是「没跑裁判」和「模型答得差」都显示成同一句「样本不足」，
全量 90 条全过也永远等不到 pass。

现在：
- `verdict` 只回答「已测范围内结论如何」，新增 `partial`（局部结论）与
  `unvalidated`（规则全过但质量证据不足）；
- `scope` 说明测了哪几条赛道、没测哪几条 —— 未测不参与判决，也不再算「样本不足」；
- `causes` 列出每条原因及其严重度：blocking（样本不足 / 规则未达 / 违例）才是失败，
  advisory（缺裁判 / 未校准）只让结论降为 unvalidated，不否决规则门禁；
- `evidence` 记录这次凭什么这么判（裁判档位、校准样本量、相关性）。

把「没证据」当成「坏消息」是评测系统最常见的两种自欺之一（另一种是把没数据显示成
绿灯）。两者都不做：缺证据就说缺证据，并且明确它影响了哪一部分结论。
"""

from __future__ import annotations

from typing import Any

from devops_agent.openjudge.benchmark import DEFAULT_TIER, Case
from devops_agent.openjudge.gates import (
    TRACK_RULES,
    calibration_reasons,
    causes_of,
    judge_level,
    overall_verdict,
    scope_of,
    track_reasons,
    track_stats,
    verdict_of,
)
from devops_agent.openjudge.metrics import (
    MAX_FAILURES,
    MAX_PARETO,
    challenge_tracks,
    failures_of,
    kpi_of,
    process_stages,
    subject_stats,
)
from devops_agent.openjudge.trace import TraceRecord


def _tier_split(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """必过集与挑战集分开判 —— 挑战集失败只影响挑战分，不否决批次。"""
    must_pass = [item for item in results if str(item.get("tier", DEFAULT_TIER)) != "challenge"]
    challenge = [item for item in results if str(item.get("tier", DEFAULT_TIER)) == "challenge"]
    return must_pass, challenge


def _track_reasons(
    tracks: dict[str, dict[str, Any]], level: str, meta: dict[str, Any]
) -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
    """只判已测赛道：未测的进 `scope`，不产生 `insufficient`。"""
    rules = {track: TRACK_RULES.get(track, {}) for track in TRACK_RULES}
    verdicts = {track: verdict_of(track, row, rules[track], level) for track, row in tracks.items()}
    reasons = {
        track: track_reasons(tracks[track], rules[track]) if track in tracks else []
        for track in TRACK_RULES
    }
    calibration_reasons(tracks, reasons, level, meta)
    return verdicts, reasons


def _track_rows(
    tracks: dict[str, dict[str, Any]],
    challenge_stats: dict[str, dict[str, Any]],
    verdicts: dict[str, str],
    reasons: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for track, row in tracks.items():
        sub = challenge_stats.get(track)
        rows[track] = {
            **row,
            "challenge": {
                "total": sub["total"] if sub else 0,
                "passed": sub["passed"] if sub else 0,
            },
            "verdict": verdicts.get(track, "insufficient"),
            "reasons": reasons.get(track, []),
        }
    return rows


def _evidence(meta: dict[str, Any], scope: dict[str, Any], level: str) -> dict[str, Any]:
    calibration = meta.get("calibration") or {}
    return {
        "judge_level": level,
        "judge_model": str(meta.get("judge_model", "")),
        "calibration_checked": bool(calibration.get("checked")),
        "calibration_n": calibration.get("n"),
        "calibration_spearman": calibration.get("spearman"),
        "measured_tracks": len(scope["measured_tracks"]),
    }


def _challenge_score(challenge: list[dict[str, Any]]) -> float | None:
    if not challenge:
        return None
    return sum(1 for item in challenge if item.get("passed")) / len(challenge)


def _truncated(pareto: dict[str, list[dict[str, Any]]], total_failures: int) -> dict[str, Any]:
    return {
        "failures": total_failures > MAX_FAILURES,
        "pareto": len(pareto["rule"]) > MAX_PARETO or len(pareto["judge"]) > MAX_PARETO,
        "total_failures": total_failures,
    }


def summarize(
    cases: list[Case],
    traces: list[TraceRecord],
    results: list[dict[str, Any]],
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成批次汇总（summary_json 的结构即前端契约）。"""
    meta = meta or {}
    level = judge_level(meta)
    must_pass, challenge = _tier_split(results)
    tracks = track_stats(must_pass)
    verdicts, reasons = _track_reasons(tracks, level, meta)
    scope = scope_of(tracks)
    failures, pareto, total_failures = failures_of(results)
    violations = sum(int(item.get("violations", 0)) for item in must_pass)
    calibration = meta.get("calibration") or {"checked": False, "valid": True}
    return {
        "verdict": overall_verdict(verdicts, violations, scope["mode"]),
        "scope": scope,
        "causes": causes_of(reasons),
        "evidence": _evidence(meta, scope, level),
        "meta": meta,
        "calibration": calibration,
        "kpi": kpi_of(results, traces),
        "tracks": _track_rows(tracks, challenge_tracks(challenge), verdicts, reasons),
        "subjects": subject_stats(results),
        "stages": process_stages(traces, results),
        "failures": failures[:MAX_FAILURES],
        "pareto": {
            "rule": pareto["rule"][:MAX_PARETO],
            "judge": pareto["judge"][:MAX_PARETO],
        },
        "truncated": _truncated(pareto, total_failures),
        "challenge_score": _challenge_score(challenge),
        "case_total": len(cases),
    }


def _head_lines(summary: dict[str, Any], scope: dict[str, Any]) -> list[str]:
    kpi = summary["kpi"]
    measured = ", ".join(scope.get("measured_tracks", [])) or "—"
    unmeasured = ", ".join(scope.get("unmeasured_tracks", [])) or "无"
    lines = [
        "# OpenJudge 评测报告",
        "",
        f"- 结论：**{summary['verdict']}**",
        f"- 范围：{scope.get('mode', '—')}；已测 {measured}；未测 {unmeasured}",
        f"- 用例：{kpi['cases']}，通过 {kpi['passed']}（{kpi['pass_rate']:.1%}）",
    ]
    if kpi.get("trials", 1) > 1:
        lines.append(
            f"- 多次试验：k={kpi['trials']}，pass@k {kpi['pass_at_k']:.1%}，"
            f"pass^k {kpi['pass_hat_k']:.1%}"
        )
    lines.extend(
        [
            f"- 安全违例：{kpi['violations']}（单列，不参与均分）",
            f"- P95 延迟：{kpi['latency_p95_ms']} ms；tokens：{kpi['tokens']}",
        ]
    )
    return lines


def _cause_lines(causes: list[dict[str, Any]]) -> list[str]:
    if not causes:
        return []
    return ["", "## 原因", ""] + [
        f"- [{item['severity']}] {item['message']}" for item in causes[:12]
    ]


def _track_lines(tracks: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "",
        "## 赛道",
        "",
        "| 赛道 | 用例 | 规则通过率 | 质量分 | 违例 | 结论 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for track, row in sorted(tracks.items()):
        judge = row.get("judge_avg")
        rate = row.get("rule_pass_rate", row["pass_rate"])
        score = "—" if judge is None else f"{judge:.2f}"
        lines.append(
            f"| {track} | {row['total']} | {rate:.0%} | {score} | "
            f"{row['violations']} | {row['verdict']} |"
        )
    return lines


def _pareto_lines(pareto: dict[str, Any]) -> list[str]:
    rows = list(pareto.get("rule") or []) + list(pareto.get("judge") or [])
    lines = ["", "## 失败 Pareto", ""]
    if not rows:
        return lines + ["无失败归因。"]
    lines.extend(["| 归因 | 次数 |", "| --- | ---: |"])
    lines.extend(f"| {item['check']} | {item['count']} |" for item in rows)
    return lines


def _failure_lines(failures: list[dict[str, Any]]) -> list[str]:
    lines = ["", "## 失败用例", ""]
    if not failures:
        return lines + ["无。"]
    for item in failures[:20]:
        dims = ", ".join(item.get("judge_dims") or []) or "—"
        checks = ", ".join(item["failed_checks"]) or "—"
        lines.append(
            f"- `{item['case_id']}`（{item['track']}）类型 {item.get('failure_type', '—')}"
            f"；断言：{checks}；裁判维度：{dims}"
        )
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    """report.md：先结论，再范围与原因，再赛道，最后失败清单。"""
    scope = summary.get("scope") or {}
    lines = _head_lines(summary, scope)
    lines += _cause_lines(summary.get("causes") or [])
    lines += _track_lines(summary["tracks"])
    lines += _pareto_lines(summary.get("pareto") or {})
    lines += _failure_lines(summary["failures"])
    return "\n".join(lines) + "\n"
