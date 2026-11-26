"""两批次的可比性判断与逐赛道 delta：纯计算，数据由 store 读出后传入。"""

from __future__ import annotations

from typing import Any


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
    tracks: dict[str, Any] = {}
    for track in sorted(set(base_tracks) | set(head_tracks)):
        old = base_tracks.get(track, {})
        new = head_tracks.get(track, {})
        tracks[track] = {
            "base_pass_rate": old.get("pass_rate"),
            "head_pass_rate": new.get("pass_rate"),
            "delta": (new.get("pass_rate", 0) - old.get("pass_rate", 0)) if old and new else None,
            "base_judge": old.get("judge_avg"),
            "head_judge": new.get("judge_avg"),
        }
    return tracks


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
