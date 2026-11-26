"""两批次的可比性判断与逐赛道 delta：纯计算，数据由 store 读出后传入。"""

from __future__ import annotations

from typing import Any

from devops_agent.openjudge import stats


def _compare_reasons(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key, label in (
        ("suite_sha", "题库"),
        ("plan_sha", "判分计划"),
        ("rubric_sha", "Rubric"),
        ("refs_sha", "参考答案"),
    ):
        if base.get(key) != head.get(key):
            reasons.append(f"{label}变化（{key}）")
    return reasons


def _track_deltas(base_tracks: dict[str, Any], head_tracks: dict[str, Any]) -> dict[str, Any]:
    """逐赛道 delta + 两比例检验。

    `mde` 保留给旧前端（1/n），新前端用 `p_value`：n=30 时 1/n≈3.3pp 会让
    「2pp 的提升」看起来显著，而两比例检验会老实说「不显著」。
    """
    return {
        track: _track_row(base_tracks.get(track, {}), head_tracks.get(track, {}))
        for track in sorted(set(base_tracks) | set(head_tracks))
    }


def _n_rate(row: dict[str, Any]) -> tuple[int, float]:
    return int(row.get("total", 0) or 0), float(row.get("pass_rate", 0.0) or 0.0)


def _track_row(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_n, old_rate = _n_rate(old)
    new_n, new_rate = _n_rate(new)
    # 一侧没有该赛道就没什么可比：delta 与检验都给 None，不给 0。
    p_value = stats.two_proportion_p(old_rate, old_n, new_rate, new_n) if old and new else None
    return {
        "base_pass_rate": old.get("pass_rate"),
        "head_pass_rate": new.get("pass_rate"),
        "base_n": old_n or None,
        "head_n": new_n or None,
        "delta": (new_rate - old_rate) if old and new else None,
        "p_value": p_value,
        "significant": None if p_value is None else p_value < 0.05,
        "base_judge": old.get("judge_avg"),
        "head_judge": new.get("judge_avg"),
    }


def _case_counts(batch: dict[str, Any]) -> int:
    return int(batch["summary"].get("kpi", {}).get("cases", 0) or 0)


def _flips(base_cases: dict[str, Any], head_cases: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"case_id": case_id, "base": base_cases[case_id], "head": head_cases[case_id]}
        for case_id in sorted(set(base_cases) & set(head_cases))
        if base_cases[case_id] != head_cases[case_id]
    ]


def compare_detail(
    base: dict[str, Any],
    head: dict[str, Any],
    base_cases: dict[str, Any],
    head_cases: dict[str, Any],
) -> dict[str, Any]:
    reasons = _compare_reasons(base, head)
    smallest = min(_case_counts(base), _case_counts(head))
    return {
        "base": base.get("id", 0),
        "head": head.get("id", 0),
        "comparable": not reasons,
        "not_comparable_reasons": reasons,
        "mde": 1.0 / smallest if smallest else None,
        "tracks": _track_deltas(
            base["summary"].get("tracks", {}), head["summary"].get("tracks", {})
        ),
        "flips": _flips(base_cases, head_cases),
    }
