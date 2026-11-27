"""记忆评测的指标与统计（SPEC §6/§7）。

所有数字来自 CaseRun 的落盘证据（探针答案、检索命中、记忆库字节），
不接受运行时自报；分母为 0 一律返回 None，让「没有样本」与「表现差」分开。
"""

from __future__ import annotations

import math
import random
from typing import Any

from devops_agent.openjudge.memory.harness import CaseRun
from devops_agent.openjudge.stats import wilson_interval


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * q) - 1)
    return ordered[index]


def _sum(runs: list[CaseRun], key: str) -> float:
    return sum(float(run.metrics.get(key, 0) or 0) for run in runs)


def aggregate_track(runs: list[CaseRun]) -> dict[str, Any]:
    """一个赛道在一个变体下的汇总；比率用计数相除，避免对比率再平均。"""
    total = len(runs)
    passed = sum(1 for run in runs if run.passed)
    constraint_total = int(_sum(runs, "constraint_probe_total"))
    return {
        "total": total,
        "passed": passed,
        "tsr": ratio(passed, total),
        "ci": wilson_interval(passed, total),
        "probe_acc": ratio(int(_sum(runs, "probe_correct")), int(_sum(runs, "probe_total"))),
        "contradiction_rate": ratio(int(_sum(runs, "constraint_probe_failed")), constraint_total),
        "recall_at_5": ratio(int(_sum(runs, "gold_retrieved")), int(_sum(runs, "gold_total"))),
        "precision_at_5": ratio(
            int(_sum(runs, "gold_hits_total")), int(_sum(runs, "injected_total"))
        ),
        "mrr": ratio(float(_sum(runs, "mrr_sum")), int(_sum(runs, "retrieval_n"))),
        "fact_retention": ratio(int(_sum(runs, "fact_retained")), int(_sum(runs, "fact_total"))),
        "constraint_retention": ratio(
            int(_sum(runs, "constraint_retained")), int(_sum(runs, "constraint_total"))
        ),
        "artifact_retention": ratio(
            int(_sum(runs, "artifact_retained")), int(_sum(runs, "artifact_total"))
        ),
        "junk_rate": ratio(int(_sum(runs, "junk_writes")), int(_sum(runs, "writes_total"))),
        "junk_injected": int(_sum(runs, "junk_injected")),
        "leak_count": int(_sum(runs, "leak_count")),
        "redaction_violation": int(_sum(runs, "redaction_violation")),
    }


def mcnemar_exact(b: int, c: int) -> float | None:
    """McNemar 精确检验（双侧）；b+c=0 时无法判断，返回 None。"""
    n = b + c
    if n == 0:
        return None
    tail: int = sum(math.comb(n, index) for index in range(0, min(b, c) + 1))
    p: float = 2.0 * tail / (2**n)
    return min(1.0, p)


def paired_bootstrap_ci(
    pairs: list[tuple[bool, bool]], *, seed: int = 13, rounds: int = 2000
) -> tuple[float, float] | None:
    """配对 bootstrap 的增益区间；样本为空返回 None。"""
    if not pairs:
        return None
    rng = random.Random(seed)
    gains: list[float] = []
    for _ in range(rounds):
        sample = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        full = sum(1 for primary, _base in sample if primary)
        base = sum(1 for _primary, baseline in sample if baseline)
        gains.append((full - base) / len(sample))
    gains.sort()
    low = gains[int(rounds * 0.025)]
    high = gains[min(rounds - 1, int(rounds * 0.975))]
    return (low, high)


def _paired_flags(
    primary_runs: list[CaseRun], baseline_runs: list[CaseRun], required: bool | None
) -> list[tuple[bool, bool]]:
    primary = {
        (run.case_id, run.trial): run.passed
        for run in primary_runs
        if required is None or run.memory_required is required
    }
    baseline = {
        (run.case_id, run.trial): run.passed
        for run in baseline_runs
        if required is None or run.memory_required is required
    }
    keys = sorted(set(primary) & set(baseline))
    return [(primary[key], baseline[key]) for key in keys]


def _empty_gain() -> dict[str, Any]:
    return {"n": 0, "full_tsr": None, "off_tsr": None, "gain_pp": None, "ci": None, "p": None}


def _gain_counts(pairs: list[tuple[bool, bool]]) -> tuple[int, int, int, int]:
    full_pass = base_pass = discordant_full = discordant_base = 0
    for full, base in pairs:
        full_pass += 1 if full else 0
        base_pass += 1 if base else 0
        if full and not base:
            discordant_full += 1
        elif base and not full:
            discordant_base += 1
    return full_pass, base_pass, discordant_full, discordant_base


def _gain_payload(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    if not pairs:
        return _empty_gain()
    full_pass, base_pass, discordant_full, discordant_base = _gain_counts(pairs)
    full_tsr = full_pass / len(pairs)
    base_tsr = base_pass / len(pairs)
    return {
        "n": len(pairs),
        "full_tsr": full_tsr,
        "off_tsr": base_tsr,
        "gain_pp": round((full_tsr - base_tsr) * 100, 1),
        "ci": paired_bootstrap_ci(pairs),
        "p": mcnemar_exact(discordant_full, discordant_base),
    }


def gain_of(primary_runs: list[CaseRun], baseline_runs: list[CaseRun]) -> dict[str, Any]:
    """同赛道、同 case/trial 的配对增益；只统计 memory_required 子集。"""
    return _gain_payload(_paired_flags(primary_runs, baseline_runs, True))


def harm_of(primary_runs: list[CaseRun], baseline_runs: list[CaseRun]) -> dict[str, Any]:
    """无记忆任务上的伤害：记忆开反而更差的比例，非劣界 1pp。"""
    pairs = _paired_flags(primary_runs, baseline_runs, False)
    if not pairs:
        return {"n": 0, "harm_rate": None}
    full_tsr = sum(1 for full, _base in pairs if full) / len(pairs)
    base_tsr = sum(1 for _full, base in pairs if base) / len(pairs)
    return {"n": len(pairs), "harm_rate": max(0.0, base_tsr - full_tsr), "full_tsr": full_tsr}


def _overheads(primary_runs: list[CaseRun], baseline_runs: list[CaseRun]) -> list[float]:
    primary = {(run.case_id, run.trial): run for run in primary_runs}
    baseline = {(run.case_id, run.trial): run for run in baseline_runs}
    values: list[float] = []
    for key in sorted(set(primary) & set(baseline)):
        off_tokens = float(baseline[key].cost.get("input_tokens", 0) or 0)
        full_tokens = float(primary[key].cost.get("input_tokens", 0) or 0)
        if off_tokens > 0:
            values.append((full_tokens - off_tokens) / off_tokens)
    return values


def cost_of(primary_runs: list[CaseRun], baseline_runs: list[CaseRun]) -> dict[str, Any]:
    """代价：token 配对比与延迟分位；没有配对样本时返回 None。"""
    latencies = [float(run.cost.get("latency_ms", 0) or 0) for run in primary_runs]
    ratios = [
        float(run.cost["compression_ratio"])
        for run in primary_runs
        if run.cost.get("compression_ratio") is not None
    ]
    overheads = _overheads(primary_runs, baseline_runs)
    return {
        "token_overhead_p50": percentile(overheads, 0.5),
        "token_overhead_p95": percentile(overheads, 0.95),
        "latency_p95_ms": percentile(latencies, 0.95),
        "memory_bytes_total": sum(
            int(run.cost.get("memory_bytes", 0) or 0) for run in primary_runs
        ),
        "compression_ratio_avg": (round(sum(ratios) / len(ratios), 3) if ratios else None),
    }
