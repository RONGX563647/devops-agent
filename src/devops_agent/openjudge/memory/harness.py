"""确定性执行器：直接驱动 MemoryRuntime，不调用模型（SPEC §14 P1）。

一条用例 = 一个独立记忆库 + 一个独立长期 scope；会话共享 scope 才能测跨会话，
但 case 之间互不可见。判分只用确定性规则：注入内容是否含要点、是否泄漏、
召回/压缩保真是否达标。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devops_agent.input.pipeline.redact import redact_preview
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.memory.tokens import estimate_tokens
from devops_agent.observability import repository as repo
from devops_agent.openjudge.memory.models import MemoryCase, Probe
from devops_agent.openjudge.memory.variants import variant_policy


@dataclass(slots=True)
class ProbeResult:
    """一次探针：注入内容（answer）即证据，不含模型自由发挥。"""

    question: str
    kind: str
    expected: dict[str, list[str]]
    answer: str
    correct: bool
    retrieved: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "kind": self.kind,
            "expected": self.expected,
            "answer": self.answer,
            "correct": self.correct,
            "retrieved": self.retrieved,
        }


@dataclass(slots=True)
class CaseRun:
    """一次（用例 × 变体 × 试验）的结果与证据。"""

    case_id: str
    track: str
    tier: str
    variant: str
    trial: int
    memory_required: bool
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    passed: bool = True
    rule_passed: bool = True
    probes: list[ProbeResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    memory_events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "track": self.track,
            "tier": self.tier,
            "variant": self.variant,
            "trial": self.trial,
            "memory_required": self.memory_required,
            "run_id": self.run_id,
            "passed": self.passed,
            "rule_passed": self.rule_passed,
            "probes": [probe.as_dict() for probe in self.probes],
            "metrics": self.metrics,
            "cost": self.cost,
            "trace": self.trace,
            "memory_events": self.memory_events,
            "error": self.error,
        }


def _correct(probe: Probe, note: str) -> bool:
    if probe.kind == "isolation":
        return all(token not in note for token in probe.expected_absent)
    return all(token in note for token in probe.expected_contains)


def _first_gold_rank(hits: list[Any], gold: tuple[str, ...]) -> float:
    for index, hit in enumerate(hits):
        if any(token in str(hit.text) for token in gold):
            return 1.0 / (index + 1)
    return 0.0


def _stored_texts(db_path: Path) -> list[str]:
    """把落盘记忆取回来做隐私扫描：只信库里的，不信运行时自报。"""
    with repo.connect(db_path) as conn:
        repo.init_db(conn)
        long_rows = conn.execute("SELECT text FROM long_term_memories").fetchall()
        card_rows = conn.execute("SELECT text, artifacts FROM session_memories").fetchall()
    texts = [str(row["text"]) for row in long_rows]
    for row in card_rows:
        texts.append(str(row["text"]))
        texts.append(str(row["artifacts"]))
    return texts


async def _privacy_audit(
    case: MemoryCase,
    run: CaseRun,
    runtime: MemoryRuntime,
    db_path: Path,
    stored: list[str],
) -> None:
    """隐私与隔离审计：原始密钥不得出现在任何落盘物；跨 scope 不得命中。"""
    policy = runtime.policy
    secrets = case.privacy
    if secrets:
        corpus = "\n".join(stored)
        corpus += "\n" + json.dumps(run.trace, ensure_ascii=False)
        corpus += "\n" + "\n".join(probe.answer for probe in run.probes)
        run.metrics["redaction_violation"] = sum(corpus.count(secret) for secret in secrets)
        leak_scope = f"{policy.user_id}:leak"
        leak_policy = policy.model_copy(update={"user_id": leak_scope})
        leak_runtime = MemoryRuntime(f"{leak_scope}:probe", leak_policy, MemorySink(), db_path)
        leak_count = 0
        for query in (*case.gold, *secrets):
            _card, hits, _error = await leak_runtime.recall(query)
            leak_count += len(hits)
        run.metrics["leak_count"] = leak_count


_METRIC_KEYS = (
    "probe_total",
    "probe_correct",
    "gold_total",
    "gold_retrieved",
    "gold_hits_total",
    "injected_total",
    "retrieval_n",
    "mrr_sum",
    "constraint_probe_total",
    "constraint_probe_failed",
    "fact_total",
    "fact_retained",
    "constraint_total",
    "constraint_retained",
    "artifact_total",
    "artifact_retained",
    "writes_total",
    "junk_writes",
    "junk_injected",
    "leak_count",
    "redaction_violation",
)


def _init_metrics(run: CaseRun) -> None:
    run.metrics.update({key: 0.0 if key == "mrr_sum" else 0 for key in _METRIC_KEYS})


def _record_turn(session_id: str, turn: Any, trace_turns: list[dict[str, Any]]) -> int:
    assistant = f"好的，已记录：{turn.text}"
    trace_turns.append(
        {
            "session": session_id,
            "user": redact_preview(turn.text),
            "assistant": redact_preview(assistant),
            "remember": turn.remember,
            "junk": turn.junk,
        }
    )
    return estimate_tokens(turn.text) + estimate_tokens(assistant)


async def _write_if_needed(runtime: MemoryRuntime, policy: Any, turn: Any, run: CaseRun) -> None:
    if not (policy.enabled and turn.remember and policy.long_term.enabled):
        return
    result = await runtime.write_turn(turn.text, f"好的，已记录：{turn.text}")
    saved = bool(result.get("stored") or result.get("deduped"))
    run.metrics["writes_total"] += 1 if saved else 0
    run.metrics["junk_writes"] += 1 if saved and turn.junk else 0


async def _drive_session(
    case: MemoryCase,
    session: Any,
    policy: Any,
    db_path: Path,
    sink: MemorySink,
    run: CaseRun,
    trace_turns: list[dict[str, Any]],
) -> tuple[MemoryRuntime, int]:
    runtime = MemoryRuntime(f"{policy.user_id}:{session.id}", policy, sink, db_path)
    tokens = 0
    for turn in session.turns:
        tokens += _record_turn(session.id, turn, trace_turns)
        await _write_if_needed(runtime, policy, turn, run)
    _maybe_compress(case, session.id, runtime, run)
    for probe in case.probes:
        if probe.after == session.id:
            await _run_probe(case, probe, runtime, run)
    return runtime, tokens


async def _drive(
    case: MemoryCase,
    policy: Any,
    db_path: Path,
    sink: MemorySink,
    run: CaseRun,
    trace_turns: list[dict[str, Any]],
) -> tuple[MemoryRuntime | None, int]:
    last: MemoryRuntime | None = None
    tokens = 0
    for session in case.sessions:
        last, used = await _drive_session(case, session, policy, db_path, sink, run, trace_turns)
        tokens += used
    return last, tokens


async def run_case(case: MemoryCase, variant: str, trial: int, tmp_dir: Path) -> CaseRun:
    """执行一条用例；任何异常都收敛为 fail，而不是让整批崩掉。"""
    run = CaseRun(
        case_id=case.id,
        track=case.track,
        tier=case.tier,
        variant=variant,
        trial=trial,
        memory_required=case.memory_required,
    )
    scope_id = f"eval:{case.id}:{variant}:{trial}"
    policy = variant_policy(variant, scope_id)
    db_path = tmp_dir / f"{case.id}-{variant}-{trial}.db"
    sink = MemorySink()
    _init_metrics(run)
    trace_turns: list[dict[str, Any]] = []
    stored: list[str] = []
    started = time.perf_counter()
    try:
        last_runtime, base_tokens = await _drive(case, policy, db_path, sink, run, trace_turns)
        if last_runtime is not None:
            stored = _stored_texts(db_path)
            await _privacy_audit(case, run, last_runtime, db_path, stored)
    except Exception as exc:  # noqa: BLE001 - 单条用例失败不阻断整批
        run.error = f"{type(exc).__name__}: {exc}"
        base_tokens = 0
    run.cost = _cost_of(case, run, base_tokens, stored, started)
    run.memory_events = sink.drain()
    run.trace = {
        "case_id": case.id,
        "run_id": run.run_id,
        "track": case.track,
        "variant": variant,
        "trial": trial,
        "sessions": trace_turns,
        "tokens": {"input": run.cost["input_tokens"], "output": 0},
        "memory_bytes": run.cost["memory_bytes"],
        "error": run.error,
    }
    _finalize(run)
    return run


def _count_in(values: Any, haystack: str) -> int:
    return sum(1 for value in values if value in haystack)


def _count_artifacts(artifacts: Any, stored: list[str], card_text: str) -> int:
    return sum(1 for item in artifacts if item in stored or item in card_text)


def _compression_counts(case: MemoryCase, compression: Any, card: Any, run: CaseRun) -> None:
    card_text = str((card or {}).get("text", ""))
    stored_artifacts = [str(item) for item in (card or {}).get("artifacts", [])]
    run.metrics["fact_total"] += len(case.gold)
    run.metrics["fact_retained"] += _count_in(case.gold, card_text)
    run.metrics["constraint_total"] += len(compression.constraints)
    run.metrics["constraint_retained"] += _count_in(compression.constraints, card_text)
    run.metrics["artifact_total"] += len(compression.artifacts)
    run.metrics["artifact_retained"] += _count_artifacts(
        compression.artifacts, stored_artifacts, card_text
    )


def _maybe_compress(
    case: MemoryCase, session_id: str, runtime: MemoryRuntime, run: CaseRun
) -> None:
    compression = case.compression
    if compression is None or compression.after != session_id:
        return
    if not runtime.policy.enabled or not runtime.policy.episodic.enabled:
        return
    card = runtime.record_compression(
        compression.summary,
        before=compression.before_tokens,
        after=compression.after_tokens,
        threshold=compression.threshold,
        trigger="eval_forced",
        artifacts=list(compression.artifacts),
    )
    _compression_counts(case, compression, card, run)


def _probe_result(probe: Probe, note: str, hits: list[Any]) -> ProbeResult:
    return ProbeResult(
        question=probe.question,
        kind=probe.kind,
        expected={
            "contains": list(probe.expected_contains),
            "absent": list(probe.expected_absent),
        },
        answer=redact_preview(note)[:1200],
        correct=_correct(probe, note),
        retrieved=[
            {"id": hit.id, "score": round(hit.score, 3), "text": redact_preview(hit.text)[:200]}
            for hit in hits
        ],
    )


def _count_probe(
    case: MemoryCase, probe: Probe, result: ProbeResult, note: str, hits: list[Any], run: CaseRun
) -> None:
    run.metrics["probe_total"] += 1
    run.metrics["probe_correct"] += 1 if result.correct else 0
    if probe.kind == "constraint":
        run.metrics["constraint_probe_total"] += 1
        run.metrics["constraint_probe_failed"] += 0 if result.correct else 1
    if case.gold:
        _count_retrieval(case, hits, run)
    if probe.kind == "isolation":
        run.metrics["junk_injected"] += sum(1 for token in probe.expected_absent if token in note)


def _count_retrieval(case: MemoryCase, hits: list[Any], run: CaseRun) -> None:
    hit_texts = [str(hit.text) for hit in hits]
    run.metrics["gold_total"] += len(case.gold)
    run.metrics["gold_retrieved"] += sum(
        1 for token in case.gold if any(token in text for text in hit_texts)
    )
    run.metrics["gold_hits_total"] += sum(
        1 for text in hit_texts if any(token in text for token in case.gold)
    )
    run.metrics["injected_total"] += len(hits)
    run.metrics["retrieval_n"] += 1
    run.metrics["mrr_sum"] += _first_gold_rank(hits, case.gold)


async def _run_probe(case: MemoryCase, probe: Probe, runtime: MemoryRuntime, run: CaseRun) -> None:
    card, hits, _error = await runtime.recall(probe.question)
    note = runtime.note(card, hits)
    result = _probe_result(probe, note, hits)
    run.probes.append(result)
    _count_probe(case, probe, result, note, hits, run)


def _cost_of(
    case: MemoryCase, run: CaseRun, base_tokens: int, stored: list[str], started: float
) -> dict[str, Any]:
    injected = sum(estimate_tokens(probe.answer) for probe in run.probes)
    latency_ms = int((time.perf_counter() - started) * 1000)
    compression = case.compression
    return {
        "input_tokens": base_tokens + injected,
        "injected_tokens": injected,
        "latency_ms": latency_ms,
        "memory_bytes": sum(len(text.encode("utf-8")) for text in stored),
        "compression_ratio": (
            round(compression.after_tokens / compression.before_tokens, 3)
            if compression and compression.before_tokens
            else None
        ),
    }


def _finalize(run: CaseRun) -> None:
    metrics = run.metrics
    run.rule_passed = (
        not run.error
        and all(probe.correct for probe in run.probes)
        and int(metrics["redaction_violation"]) == 0
        and int(metrics["leak_count"]) == 0
    )
    run.passed = run.rule_passed
    run.metrics["pass_rate"] = 1.0 if run.passed else 0.0
