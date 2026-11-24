"""OpenJudge CLI：validate / run / grade / compare / rubric。

`run` 是唯一会驱动真实模型的入口；`grade` 只读轨迹重判，换 plan/rubric 不重跑。
PR 门禁用 `run --no-judge --offline-only`（只跑输入管线与确定性断言），
夜间用完整批次。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from devops_agent.env import load_env
from devops_agent.openjudge import benchmark, grade, report, sandbox, store
from devops_agent.openjudge.trace import TraceRecord, load_traces, prompt_sha

CASES_DIR = Path(__file__).resolve().parents[3] / "eval" / "openjudge" / "cases"


def _case_paths(target: str | None) -> list[Path]:
    path = Path(target) if target else CASES_DIR
    if path.is_dir():
        return sorted(path.glob("*.jsonl"))
    return [path]


def _load(target: str | None, case_id: str = "", tracks: str = "") -> list[benchmark.Case]:
    cases = benchmark.load_cases(_case_paths(target))
    if case_id:
        cases = [case for case in cases if case.id == case_id]
        if not cases:
            raise SystemExit(f"未找到用例 {case_id}")
    if tracks:
        allowed = {item.strip().upper() for item in tracks.split(",") if item.strip()}
        cases = [case for case in cases if case.track in allowed]
    return cases


async def _capture(
    cases: list[benchmark.Case], *, suite: str, concurrency: int, timeout: float
) -> list[TraceRecord]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(case: benchmark.Case) -> TraceRecord:
        async with semaphore:
            return cast(TraceRecord, await harness_run(case, suite=suite, timeout=timeout))

    return list(await asyncio.gather(*(one(case) for case in cases)))


def harness_run(case: benchmark.Case, *, suite: str, timeout: float) -> Any:
    from devops_agent.openjudge.harness import run_case

    return run_case(case, suite=suite, timeout=timeout)


def _meta(cases: list[benchmark.Case], plan: grade.Plan, suite: str) -> dict[str, Any]:
    from importlib.metadata import version

    try:
        oj_version = version("py-openjudge")
    except Exception:  # pragma: no cover - 可选依赖
        oj_version = ""
    return {
        "suite_sha": suite,
        "plan": plan.name,
        "plan_sha": grade.plan_sha(plan),
        "rubric_sha": "",
        "refs_sha": "",
        "prompt_sha": prompt_sha(""),
        "model": os.environ.get("DEVOPS_AGENT_MODEL", ""),
        "judge_model": os.environ.get("DEVOPS_AGENT_JUDGE_MODEL")
        or os.environ.get("DEVOPS_AGENT_MODEL", ""),
        "oj_version": oj_version,
        "config_id": cases[0].config_id if cases else "",
        "case_count": len(cases),
    }


def _persist(
    cases: list[benchmark.Case],
    traces: list[TraceRecord],
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    out_dir: Path,
    db: Path | None,
    started: str,
) -> int | None:
    with store.connect(db) as conn:
        store.init_db(conn)
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
        )
        for trace in traces:
            result = next((item for item in results if item["case_id"] == trace.case_id), None)
            if result is not None:
                store.insert_case(conn, batch_id, result=result, trace=trace.to_dict())
    return batch_id


def _write(out_dir: Path, traces: list[TraceRecord], summary: dict[str, Any]) -> None:
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


def _select_cases(args: argparse.Namespace) -> list[Any]:
    cases = _load(args.cases, args.case, args.track)
    if args.offline_only:
        cases = [case for case in cases if case.offline]
    if args.limit:
        cases = cases[: args.limit]
    return cases


def _optional_path(arg: str | None) -> Path | None:
    return Path(arg) if arg else None


async def _run(args: argparse.Namespace) -> int:
    cases = _select_cases(args)
    if not cases:
        print("没有可执行的用例", file=sys.stderr)
        return 2
    suite = benchmark.suite_sha(cases)
    plan = grade.load_plan(Path(args.plan) if args.plan else None)
    started = datetime.now(UTC).isoformat()
    traces = await _capture(cases, suite=suite, concurrency=args.concurrency, timeout=args.timeout)
    try:
        results = await grade.grade_traces(cases, traces, plan=plan, judge=not args.no_judge)
    except grade.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    meta = _meta(cases, plan, suite)
    summary = report.summarize(cases, traces, results, meta=meta)
    out_dir = Path(args.out_dir) if args.out_dir else Path("workspace/evals/openjudge/latest")
    _write(out_dir, traces, summary)
    batch_id = _persist(cases, traces, results, summary, out_dir, _optional_path(args.db), started)
    kpi = summary["kpi"]
    print(
        f"batch={batch_id} verdict={summary['verdict']} "
        f"passed={kpi['passed']}/{kpi['cases']} violations={kpi['violations']} "
        f"judge_n={kpi['judge_n']} out={out_dir}"
    )
    for track, stats in sorted(summary["tracks"].items()):
        print(f"  {track}: {stats['passed']}/{stats['total']} {stats['verdict']}")
    return 0 if summary["verdict"] == "pass" else 1


async def _grade(args: argparse.Namespace) -> int:
    cases = _load(args.cases)
    traces = load_traces(Path(args.traces))
    plan = grade.load_plan(Path(args.plan) if args.plan else None)
    try:
        results = await grade.grade_traces(cases, traces, plan=plan, judge=not args.no_judge)
    except grade.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    suite = benchmark.suite_sha(cases)
    summary = report.summarize(cases, traces, results, meta=_meta(cases, plan, suite))
    out_dir = Path(args.out_dir) if args.out_dir else Path("workspace/evals/openjudge/regrade")
    _write(out_dir, traces, summary)
    print(f"verdict={summary['verdict']} out={out_dir}")
    return 0 if summary["verdict"] == "pass" else 1


def _validate(args: argparse.Namespace) -> int:
    cases = _load(args.cases)
    problems = benchmark.validate(cases, sandbox.fixture_root(), strict=args.strict)
    coverage = benchmark.coverage(cases)
    print(
        json.dumps(
            {"cases": len(cases), "coverage": coverage, "problems": problems},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if problems else 0


def _compare(args: argparse.Namespace) -> int:
    with store.connect(Path(args.db) if args.db else None) as conn:
        store.init_db(conn)
        payload = store.compare(conn, args.base, args.head)
    if payload is None:
        print("批次不存在", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


async def _rubric(args: argparse.Namespace) -> int:
    from devops_agent.openjudge import rubric

    if args.action == "verify":
        payload = rubric.load(args.name, args.version)
        print(json.dumps({"sha": payload["sha"], "meta": payload["meta"]}, ensure_ascii=False))
        return 0
    samples = []
    if args.samples:
        samples = [
            json.loads(line)["text"]
            for line in Path(args.samples).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    folder = await rubric.generate(
        args.name,
        task_description=args.task,
        scenario=args.scenario,
        samples=samples,
        version=args.version,
    )
    print(f"draft rubric: {folder}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openjudge", description="DevOps-Agent OpenJudge 评测")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验题库与覆盖率")
    validate.add_argument("--cases")
    validate.add_argument("--strict", action="store_true")
    validate.set_defaults(func=_validate)

    run = sub.add_parser("run", help="采集轨迹并判分")
    run.add_argument("--cases")
    run.add_argument("--case")
    run.add_argument("--track")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--timeout", type=float, default=180.0)
    run.add_argument("--plan")
    run.add_argument("--db")
    run.add_argument("--out-dir")
    run.add_argument("--no-judge", action="store_true")
    run.add_argument("--offline-only", action="store_true")
    run.set_defaults(func=lambda args: asyncio.run(_run(args)))

    regrade = sub.add_parser("grade", help="用已有轨迹重判")
    regrade.add_argument("--traces", required=True)
    regrade.add_argument("--cases")
    regrade.add_argument("--plan")
    regrade.add_argument("--out-dir")
    regrade.add_argument("--no-judge", action="store_true")
    regrade.set_defaults(func=lambda args: asyncio.run(_grade(args)))

    compare = sub.add_parser("compare", help="比较两个批次")
    compare.add_argument("--base", type=int, required=True)
    compare.add_argument("--head", type=int, required=True)
    compare.add_argument("--db")
    compare.set_defaults(func=_compare)

    rubric = sub.add_parser("rubric", help="生成或校验 Rubric")
    rubric.add_argument("action", choices=["generate", "verify"])
    rubric.add_argument("--name", default="repo_qa_quality")
    rubric.add_argument("--version", default="v1")
    rubric.add_argument(
        "--task", default="基于真实文件回答，给出路径:行号，找不到就明说，不得编造。"
    )
    rubric.add_argument("--scenario", default="仓库结构/函数/配置问答，回答直接用于排障。")
    rubric.add_argument("--samples")
    rubric.set_defaults(func=lambda args: asyncio.run(_rubric(args)))
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
