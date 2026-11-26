"""OpenJudge 批次执行：CLI 与在线评测共用同一条流水线。

为什么要抽出来：原来的 `cli._run` 把采集、判分、落库、写报告揉在一个函数里。
在线评测若再写一份，两边的判定规则（judge 开关、并发、超时、取消时机）迟早分叉，
而分叉的后果是「页面上的结论和 CI 的结论不一致」——评测工具最不该出的问题。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devops_agent.openjudge import benchmark, grade, report, settings, store
from devops_agent.openjudge.trace import TraceRecord, prompt_sha

DEFAULT_OUT_DIR = Path("workspace/evals/openjudge/latest")

#: (已完成, 总数, 刚跑完的用例 id)
Progress = Callable[[int, int, str], None]
#: 在两条用例之间询问是否该停；返回 True 时收尾并抛 RunCancelled
StopCheck = Callable[[], bool]


class RunCancelled(Exception):
    """批次被调用方（用户）中途取消。"""


@dataclass(slots=True)
class RunOptions:
    judge: bool = True
    concurrency: int = 4
    timeout: float = 180.0
    plan: Path | None = None
    db: Path | None = None
    out_dir: Path | None = None
    #: 空字符串 = 跟随界面配置 / 全局模型配置（由 `settings.resolved()` 决定）
    target_model: str = ""
    judge_model: str = ""


@dataclass(slots=True)
class RunResult:
    batch_id: int | None
    summary: dict[str, Any]
    out_dir: Path
    traces: list[TraceRecord] = field(default_factory=list)


async def _capture(
    cases: list[benchmark.Case],
    *,
    suite: str,
    concurrency: int,
    timeout: float,
    progress: Progress | None,
    should_stop: StopCheck | None,
    target_model: str = "",
) -> list[TraceRecord]:
    """固定并发抓轨迹。取消只在两条用例之间检查，不打断已开始的那一条。"""
    from devops_agent.openjudge.harness import run_case

    total = len(cases)
    traces: list[TraceRecord] = []
    cursor = 0
    done = 0

    async def worker() -> None:
        nonlocal cursor, done
        # step1: worker 共享 cursor，取值即自增
        while cursor < total:
            if should_stop is not None and should_stop():
                return
            case = cases[cursor]
            cursor += 1
            # step2: 单例失败由 run_case 写 status，不外抛
            trace = await run_case(case, suite=suite, timeout=timeout, target_model=target_model)
            traces.append(trace)
            done += 1
            if progress is not None:
                progress(done, total, case.id)

    # step3: 并发启动 worker；取消只在用例间生效
    await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
    return traces


async def execute(
    cases: list[benchmark.Case],
    *,
    options: RunOptions | None = None,
    progress: Progress | None = None,
    should_stop: StopCheck | None = None,
) -> RunResult:
    """跑一批：采集 → 判分 → 汇总 → 落盘 → 入库。取消时抛 `RunCancelled`。"""
    if not cases:
        raise ValueError("没有可执行的用例")
    opts = options or RunOptions()
    # 模型只解析一次：采集与判分必须用同一份结论，否则「换了模型」会记成两个来源。
    # step1: 固定题库指纹与判分计划，进批次元数据
    models = settings.resolved(opts.target_model, opts.judge_model)
    suite = benchmark.suite_sha(cases)
    plan = grade.load_plan(opts.plan)
    rubric_sha = grade.rubric_sha(plan)
    started = datetime.now(UTC).isoformat()
    # step1b: 裁判校准门禁；未校准时只报告不参与通过判定
    with store.connect(opts.db) as conn:
        store.init_db(conn)
        calibration = store.calibration_gate(
            conn,
            metrics=list(plan.judges),
            judge_model=models["judge_model"],
            rubric_sha=rubric_sha,
        )
    # step2: 采集轨迹（唯一驱动模型的一步）
    traces = await _capture(
        cases,
        suite=suite,
        concurrency=opts.concurrency,
        timeout=opts.timeout,
        progress=progress,
        should_stop=should_stop,
        target_model=models["target_model"] if opts.target_model else "",
    )
    if should_stop is not None and should_stop():
        raise RunCancelled
    # step3: 规则门禁全覆盖，语义裁判仅规则通过者
    results = await grade.grade_traces(
        cases,
        traces,
        plan=plan,
        judge=opts.judge,
        judge_model_name=models["judge_model"],
        judge_gate=calibration["valid"],
    )
    meta = meta_of(cases, plan, suite, models=models, calibration=calibration)
    summary = report.summarize(cases, traces, results, meta=meta)
    out_dir = opts.out_dir or DEFAULT_OUT_DIR
    write_artifacts(out_dir, traces, summary)
    # step4: 汇总 + 写盘 + 入库，返回 batch_id
    batch_id = persist(cases, traces, results, summary, db=opts.db, started=started)
    return RunResult(batch_id=batch_id, summary=summary, out_dir=out_dir, traces=traces)


def meta_of(
    cases: list[benchmark.Case],
    plan: grade.Plan,
    suite: str,
    *,
    models: dict[str, str] | None = None,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """批次可比性元数据：题库/计划/Rubric/模型任一变化都应让两次结果不可直接比较。

    `models` 省略时读界面配置 + 环境变量 —— CLI 与在线跑因而不会记出两套模型名。
    """
    from importlib.metadata import version

    try:
        oj_version = version("py-openjudge")
    except Exception:  # pragma: no cover - 可选依赖
        oj_version = ""
    active = models or settings.resolved()
    status = calibration or {"checked": False, "valid": True, "missing": [], "calibration_ids": []}
    ids = status.get("calibration_ids") or []
    return {
        "suite_sha": suite,
        "plan": plan.name,
        "plan_sha": grade.plan_sha(plan),
        "rubric_sha": grade.rubric_sha(plan),
        "calibration": status,
        "calibration_id": ids[0] if ids else None,
        "refs_sha": "",
        "prompt_sha": prompt_sha(""),
        "model": active["target_model"],
        "judge_model": active["judge_model"],
        "oj_version": oj_version,
        "config_id": cases[0].config_id if cases else "",
        "case_count": len(cases),
    }


def write_artifacts(out_dir: Path, traces: list[TraceRecord], summary: dict[str, Any]) -> None:
    """轨迹全量、summary 与可读报告三件套，供离线复判与归档。

    traces 必须全量落盘：`grade --traces` 靠它重判，缺一条就少一条证据。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_file = out_dir / "traces.jsonl"
    trace_file.write_text(
        "".join(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n" for trace in traces),
        encoding="utf-8",
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(report.render_markdown(summary), encoding="utf-8")


def persist(
    cases: list[benchmark.Case],
    traces: list[TraceRecord],
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    db: Path | None,
    started: str,
) -> int | None:
    """一个批次 + 每条用例一行；trace 原样存，便于换 plan 重判。"""
    del cases  # 用例形态已在 summary/results 里，签名保留便于调用方对齐
    with store.connect(db) as conn:
        store.init_db(conn)
        # step1: 写批次头拿 batch_id
        batch_id = store.insert_batch(
            conn,
            suite_sha=str(summary["meta"].get("suite_sha", "")),
            plan_sha=str(summary["meta"].get("plan_sha", "")),
            rubric_sha=str(summary["meta"].get("rubric_sha", "")),
            prompt_sha=str(summary["meta"].get("prompt_sha", "")),
            model=str(summary["meta"].get("model", "")),
            oj_version=str(summary["meta"].get("oj_version", "")),
            config_id=str(summary["meta"].get("config_id", "")),
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(),
            verdict=str(summary["verdict"]),
            summary=summary,
            calibration_id=summary["meta"].get("calibration_id"),
        )
        # step2: 按 case_id 配对结果与轨迹
        by_case = {item["case_id"]: item for item in results}
        # step3: 逐条落库，无判分结果的跳过
        for trace in traces:
            result = by_case.get(trace.case_id)
            if result is not None:
                store.insert_case(conn, batch_id, result=result, trace=trace.to_dict())
    return batch_id
