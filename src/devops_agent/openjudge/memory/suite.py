"""记忆题库解析与计划加载（SPEC-0014 §5/§12）。

题库是数据：一行 JSON 一条用例，字段稳定可 diff。校验在 `validate` 里做，
任何「探针引用了不存在的会话」「gold 为空」都会在加载期暴露，而不是跑出假绿。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from devops_agent.openjudge.memory.models import (
    DEFAULT_TIER,
    TIERS,
    TRACKS,
    Compression,
    MemoryCase,
    MemoryPlan,
    Probe,
    Session,
    Turn,
    VariantSpec,
)

PROBE_KINDS = ("recall", "constraint", "isolation")


def _turn(raw: Any, case_id: str) -> Turn:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError(f"{case_id}: 用户轮不能为空")
        return Turn(text=text)
    if not isinstance(raw, dict):
        raise ValueError(f"{case_id}: turn 必须是字符串或对象")
    text = str(raw.get("text", "")).strip()
    if not text:
        raise ValueError(f"{case_id}: 用户轮不能为空")
    return Turn(
        text=text,
        remember=bool(raw.get("remember", True)),
        junk=bool(raw.get("junk", False)),
    )


def _session(raw: Any, case_id: str) -> Session:
    if not isinstance(raw, dict) or not raw.get("id"):
        raise ValueError(f"{case_id}: session 缺少 id")
    turns_raw = raw.get("turns")
    if not isinstance(turns_raw, list) or not turns_raw:
        raise ValueError(f"{case_id}: session {raw.get('id')} 没有 turns")
    return Session(id=str(raw["id"]), turns=tuple(_turn(item, case_id) for item in turns_raw))


def _probe(raw: Any, case_id: str) -> Probe:
    if not isinstance(raw, dict) or not raw.get("question"):
        raise ValueError(f"{case_id}: probe 缺少 question")
    kind = str(raw.get("kind", "recall"))
    if kind not in PROBE_KINDS:
        raise ValueError(f"{case_id}: 未知 probe kind {kind!r}")
    return Probe(
        after=str(raw.get("after", "")),
        question=str(raw["question"]),
        kind=kind,
        expected_contains=tuple(str(x) for x in raw.get("expected_contains", ())),
        expected_absent=tuple(str(x) for x in raw.get("expected_absent", ())),
    )


def _compression(raw: Any, case_id: str) -> Compression | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{case_id}: compression 必须是对象")
    summary = str(raw.get("summary", "")).strip()
    if not summary:
        raise ValueError(f"{case_id}: compression.summary 不能为空")
    return Compression(
        after=str(raw.get("after", "")),
        summary=summary,
        before_tokens=int(raw.get("before_tokens", 0)),
        after_tokens=int(raw.get("after_tokens", 0)),
        threshold=int(raw.get("threshold", 0)),
        artifacts=tuple(str(x) for x in raw.get("artifacts", ())),
        constraints=tuple(str(x) for x in raw.get("constraints", ())),
    )


def _require_id(raw: dict[str, Any]) -> str:
    case_id = str(raw.get("id", "")).strip()
    if not case_id:
        raise ValueError("用例缺少 id")
    return case_id


def _enum(value: Any, allowed: tuple[str, ...], case_id: str, label: str) -> str:
    text = str(value)
    if text not in allowed:
        raise ValueError(f"{case_id}: 未知{label} {text!r}")
    return text


def _sessions(raw: Any, case_id: str) -> tuple[Session, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{case_id}: sessions 不能为空")
    return tuple(_session(item, case_id) for item in raw)


def _check_refs(
    case_id: str, sessions: tuple[Session, ...], probes: tuple[Probe, ...], compression: Any
) -> None:
    ids = {session.id for session in sessions}
    for probe in probes:
        if probe.after and probe.after not in ids:
            raise ValueError(f"{case_id}: probe.after {probe.after!r} 不存在")
    if compression and compression.after and compression.after not in ids:
        raise ValueError(f"{case_id}: compression.after {compression.after!r} 不存在")


def parse_case(raw: dict[str, Any]) -> MemoryCase:
    """把一行 JSON 解析成 MemoryCase；结构错误带 case id 抛出。"""
    case_id = _require_id(raw)
    track = _enum(raw.get("track", ""), TRACKS, case_id, "赛道")
    tier = _enum(raw.get("tier", DEFAULT_TIER), TIERS, case_id, "分层")
    sessions = _sessions(raw.get("sessions"), case_id)
    probes = tuple(_probe(item, case_id) for item in raw.get("probes", ()))
    compression = _compression(raw.get("compression"), case_id)
    _check_refs(case_id, sessions, probes, compression)
    return MemoryCase(
        id=case_id,
        track=track,
        tier=tier,
        memory_required=bool(raw.get("memory_required", True)),
        tags=tuple(str(item) for item in raw.get("tags", ())),
        sessions=sessions,
        probes=probes,
        privacy=tuple(str(item) for item in raw.get("privacy", ())),
        compression=compression,
        gold=tuple(str(item) for item in raw.get("gold", ())),
    )


def _case_paths(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.glob("*.jsonl"))
    return [target]


def load_cases(target: Path) -> list[MemoryCase]:
    """按路径顺序读取 JSONL；空行忽略，重复 id 直接报错。"""
    cases: list[MemoryCase] = []
    seen: set[str] = set()
    for path in _case_paths(target):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: JSON 解析失败: {exc}") from exc
            case = parse_case(raw)
            if case.id in seen:
                raise ValueError(f"{case.id}: 用例 id 重复")
            seen.add(case.id)
            cases.append(case)
    return cases


def _canonical(case: MemoryCase) -> dict[str, Any]:
    compression = case.compression
    return {
        "id": case.id,
        "track": case.track,
        "tier": case.tier,
        "memory_required": case.memory_required,
        "sessions": [
            {"id": session.id, "turns": [(t.text, t.remember, t.junk) for t in session.turns]}
            for session in case.sessions
        ],
        "probes": [
            (p.after, p.question, p.kind, p.expected_contains, p.expected_absent)
            for p in case.probes
        ],
        "privacy": case.privacy,
        "compression": (
            (
                compression.after,
                compression.summary,
                compression.artifacts,
                compression.constraints,
            )
            if compression
            else None
        ),
        "gold": case.gold,
    }


def suite_sha(cases: list[MemoryCase]) -> str:
    """题库指纹：按 id 排序后规范化 JSON 的 sha256。"""
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda item: item.id):
        digest.update(json.dumps(_canonical(case), sort_keys=True, ensure_ascii=False).encode())
    return digest.hexdigest()


def _probe_problems(case: MemoryCase, probe: Probe) -> list[str]:
    if probe.kind == "isolation":
        return (
            [f"{case.id}: isolation 探针缺少 expected_absent"] if not probe.expected_absent else []
        )
    return (
        [f"{case.id}: {probe.kind} 探针缺少 expected_contains"]
        if not probe.expected_contains
        else []
    )


def _case_problems(case: MemoryCase) -> list[str]:
    problems: list[str] = []
    if not case.sessions:
        problems.append(f"{case.id}: 没有会话")
    if not case.probes:
        problems.append(f"{case.id}: 没有探针")
    for probe in case.probes:
        problems.extend(_probe_problems(case, probe))
    if case.memory_required and not case.gold:
        problems.append(f"{case.id}: 记忆必需用例缺少 gold")
    if case.track == "PR" and not case.privacy:
        problems.append(f"{case.id}: PR 赛道必须声明 privacy secret")
    return problems


def validate(cases: list[MemoryCase]) -> list[str]:
    """题库自洽性：赛道/分层合法、探针可判、gold 非空（记忆必需赛道）。"""
    problems: list[str] = []
    for case in cases:
        problems.extend(_case_problems(case))
    return problems


def _resolve_suite(plan_path: Path, suite: str) -> Path:
    raw = Path(suite)
    if raw.is_absolute():
        return raw
    for base in (plan_path.parent, plan_path.parent.parent, plan_path.parents[2]):
        if (base / raw).exists():
            return base / raw
    return plan_path.parents[2] / raw


def _variant_name(value: Any) -> str:
    """YAML 1.1 会把未加引号的 off/on 解析成布尔；写错也要得到稳定的变体名。"""
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def _variants(raw: Any) -> dict[str, VariantSpec]:
    variants: dict[str, VariantSpec] = {}
    for name, overrides in (raw or {}).items():
        key = _variant_name(name)
        variants[key] = VariantSpec(name=key, overrides=dict(overrides or {}))
    if not variants:
        raise ValueError("plan.variants 不能为空")
    return variants


def load_plan(path: Path) -> MemoryPlan:
    """读取 plan.yaml；缺失字段用安全默认值，不静默放宽门禁。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("plan.yaml 必须是对象")
    stats = raw.get("stats") or {}
    budget = raw.get("budget") or {}
    thresholds = {str(key): float(value) for key, value in (raw.get("thresholds") or {}).items()}
    return MemoryPlan(
        name=str(raw.get("plan", path.stem)),
        suite_dir=_resolve_suite(path, str(raw.get("suite", "cases"))),
        baseline=_variant_name(raw.get("baseline", "off")),
        primary=_variant_name(raw.get("primary", "full")),
        variants=_variants(raw.get("variants")),
        trials=int(raw.get("trials", 1)),
        min_n=int(raw.get("min_n", 1)),
        thresholds=thresholds,
        require_ci=bool(stats.get("require_ci", False)),
        alpha=float(stats.get("alpha", 0.05)),
        cost_enforced=bool(raw.get("cost_enforced", False)),
        red_lines=tuple(str(x) for x in raw.get("red_lines", ())),
        max_tokens=int(budget.get("max_tokens", 0)),
        max_minutes=int(budget.get("max_minutes", 0)),
    )


def plan_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
