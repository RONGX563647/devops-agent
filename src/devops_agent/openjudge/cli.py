"""OpenJudge CLI：validate / run / grade / compare / rubric。

`run` 是唯一会驱动真实模型的入口；`grade` 只读轨迹重判，换 plan/rubric 不重跑。
PR 门禁用 `run --no-judge --offline-only`（只跑输入管线与确定性断言），
夜间用完整批次。

执行本身在 `run.execute`（与在线评测共用），这里只负责参数解析与退出码。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from devops_agent.env import load_env
from devops_agent.openjudge import (
    benchmark,
    casestore,
    grade,
    report,
    run,
    sandbox,
    store,
)
from devops_agent.openjudge.trace import load_traces

#: `build_parser` 里的子命令变量叫 run，会遮蔽模块，先把边界值固定下来。
MAX_TRIALS = run.MAX_TRIALS

DEFAULT_BASELINE = casestore.REPO_CASES.parent / "coverage.json"


def _case_paths(target: str) -> list[Path]:
    path = Path(target)
    if path.is_dir():
        return sorted(path.glob("*.jsonl"))
    return [path]


def _load(target: str | None, case_id: str = "", tracks: str = "") -> list[benchmark.Case]:
    """用例来源：显式路径优先；否则「仓库 + 本地覆盖层」。

    覆盖层是在线改用例的落点（`workspace/openjudge/cases/`）。CLI 也读它，否则
    「页面上改的题」和「命令行跑的题」会是两份，改完立刻对不上。
    """
    cases = benchmark.load_cases(_case_paths(target)) if target else casestore.load_cases()
    return _filter_track(_filter_case(cases, case_id), tracks)


def _filter_case(cases: list[benchmark.Case], case_id: str) -> list[benchmark.Case]:
    """按 id 取单条；写错 id 直接退出，而不是静默跑 0 条让人以为跑过了。"""
    if not case_id:
        return cases
    picked = [case for case in cases if case.id == case_id]
    if not picked:
        raise SystemExit(f"未找到用例 {case_id}")
    return picked


def _filter_track(cases: list[benchmark.Case], tracks: str) -> list[benchmark.Case]:
    """按赛道过滤；空串表示不过滤（区别于「过滤后为空」）。"""
    if not tracks:
        return cases
    allowed = {item.strip().upper() for item in tracks.split(",") if item.strip()}
    return [case for case in cases if case.track in allowed]


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
    """采集 + 判分一次；退出码 0 通过 / 1 未通过 / 2 配置或没有用例。"""
    # step1: 按 case/track/limit 筛用例
    cases = _select_cases(args)
    if not cases:
        print("没有可执行的用例", file=sys.stderr)
        return 2
    # step2: 参数转 RunOptions 交 run.execute
    options = run.RunOptions(
        judge=not args.no_judge,
        concurrency=args.concurrency,
        timeout=args.timeout,
        trials=getattr(args, "trials", 1),
        plan=_optional_path(args.plan),
        db=_optional_path(args.db),
        out_dir=_optional_path(args.out_dir),
        target_model=args.target_model or "",
        judge_model=args.judge_model or "",
    )
    try:
        result = await run.execute(cases, options=options)
    except grade.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    summary = result.summary
    kpi = summary["kpi"]
    print(
        f"batch={result.batch_id} verdict={summary['verdict']} "
        f"passed={kpi['passed']}/{kpi['cases']} violations={kpi['violations']} "
        f"judge_n={kpi['judge_n']} out={result.out_dir}"
    )
    for track, stats in sorted(summary["tracks"].items()):
        print(f"  {track}: {stats['passed']}/{stats['total']} {stats['verdict']}")
    # step3: 非 pass 返回非零阻断 CI
    return 0 if summary["verdict"] == "pass" else 1


async def _grade(args: argparse.Namespace) -> int:
    from devops_agent.openjudge import settings

    cases = _load(args.cases)
    traces = load_traces(Path(args.traces))
    plan = grade.load_plan(_optional_path(args.plan))
    judge_model = args.judge_model or ""
    if judge_model:
        print(f"裁判模型：{judge_model}")
    # 重判也要读校准档位：否则同一批判分结果在 CLI 里永远 unvalidated。
    with store.connect(_optional_path(args.db)) as conn:
        store.init_db(conn)
        calibration = store.calibration_gate(
            conn,
            metrics=list(plan.judges),
            judge_model=judge_model or settings.resolved()["judge_model"],
            rubric_sha=grade.rubric_sha(plan),
        )
    try:
        results = await grade.grade_traces(
            cases,
            traces,
            plan=plan,
            judge=not args.no_judge,
            judge_model_name=judge_model,
            judge_level=str(calibration.get("level", "off")),
        )
    except grade.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    suite = benchmark.suite_sha(cases)
    summary = report.summarize(
        cases,
        traces,
        results,
        meta=run.meta_of(cases, plan, suite, calibration=calibration),
    )
    out_dir = Path(args.out_dir) if args.out_dir else Path("workspace/evals/openjudge/regrade")
    run.write_artifacts(out_dir, traces, summary)
    print(f"verdict={summary['verdict']} out={out_dir}")
    return 0 if summary["verdict"] == "pass" else 1


def _validate(args: argparse.Namespace) -> int:
    """校验题库并比对覆盖基线；有问题返回 1。

    覆盖基线描述的是**仓库题库**：本地覆盖层是实验，不参与「覆盖率是否回退」。
    所以不带 `--cases` 时用 `repo_cases()` 而不是 `load_cases()` —— 否则在页面上
    删掉一条题，CI 的覆盖率棘轮就会假红。
    """
    cases = _load(args.cases) if args.cases else casestore.repo_cases()
    # step1: validate 查单条，strict 查配额
    problems = benchmark.validate(cases, sandbox.fixture_root(), strict=args.strict)
    coverage = benchmark.coverage(cases)
    baseline = Path(args.baseline) if args.baseline else DEFAULT_BASELINE
    # step2: update-baseline 只写不比回退
    if args.update_baseline:
        baseline.write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"已写入覆盖基线 {baseline}")
    # step3: 临时题库跳基线
    elif args.cases:
        # 基线描述的是仓库自带题库；校验一份临时题库时不应拿它做回退判断。
        pass
    # step4: 其余读基线，覆盖降即问题
    elif baseline.exists():
        declared = json.loads(baseline.read_text(encoding="utf-8"))
        problems.extend(benchmark.compare_coverage(coverage, declared))
    else:
        problems.append(f"缺少覆盖基线 {baseline}（用 --update-baseline 生成）")
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


def _calibration(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else [payload]
    with store.connect() as conn:
        store.init_db(conn)
        for item in items:
            store.insert_calibration(
                conn,
                metric=str(item["metric"]),
                judge_model=str(item["judge_model"]),
                rubric_sha=str(item.get("rubric_sha", "")),
                n=int(item["n"]),
                spearman=float(item["spearman"]),
                active=bool(item.get("active", True)),
                reviewer=str(item.get("reviewer", "")),
                meta=dict(item.get("meta") or {}),
            )
    print(f"imported {len(items)} calibration record(s)")
    return 0


async def _rubric(args: argparse.Namespace) -> int:
    from devops_agent.openjudge import rubric

    if args.action == "verify":
        payload = rubric.load(args.name, args.version)
        print(json.dumps({"sha": payload["sha"], "meta": payload["meta"]}, ensure_ascii=False))
        return 0
    if args.action == "approve":
        try:
            payload = rubric.approve(args.name, args.version, reviewer=args.reviewer)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(payload, ensure_ascii=False))
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


async def _memory_run(args: argparse.Namespace) -> int:
    """跑一次确定性记忆批次；退出码 0 通过 / 1 未通过 / 2 题库或计划错误。"""
    from devops_agent.openjudge.memory import runner as memory_runner

    try:
        result = await memory_runner.run_plan(
            Path(args.plan),
            db_path=Path(args.db) if args.db else None,
            suite_tier=args.suite,
            variants=args.variant or None,
        )
    except memory_runner.BatchError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    print(f"batch={result.batch_id} verdict={result.verdict}")
    for track, row in sorted(result.summary.get("tracks", {}).items()):
        print(
            f"  {track}: {row['passed']}/{row['total']} "
            f"tsr={_fmt(row['tsr'])} probe_acc={_fmt(row['probe_acc'])}"
        )
    return 0 if result.verdict == "pass" else 1


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _memory_list(args: argparse.Namespace) -> int:
    from devops_agent.openjudge.memory import store as memory_store

    with store.connect(Path(args.db) if args.db else None) as conn:
        store.init_db(conn)
        memory_store.init_db(conn)
        rows = memory_store.list_batches(conn, limit=args.limit)
    payload = [
        {"id": row["id"], "verdict": row["verdict"], "finished_at": row["finished_at"]}
        for row in rows
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openjudge", description="DevOps-Agent OpenJudge 评测")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验题库与覆盖率")
    validate.add_argument("--cases")
    validate.add_argument("--strict", action="store_true")
    validate.add_argument("--baseline", help="覆盖基线 JSON，默认 eval/openjudge/coverage.json")
    validate.add_argument(
        "--update-baseline",
        action="store_true",
        help="按当前题库重写覆盖基线（只应随 PR 显式提升）",
    )
    validate.set_defaults(func=_validate)

    run = sub.add_parser("run", help="采集轨迹并判分")
    run.add_argument("--cases")
    run.add_argument("--case")
    run.add_argument("--track")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--timeout", type=float, default=180.0)
    run.add_argument(
        "--trials",
        type=int,
        default=1,
        help=f"每条用例重复次数（1..{MAX_TRIALS}）；>1 才能区分稳定通过",
    )
    run.add_argument("--plan")
    run.add_argument("--db")
    run.add_argument("--out-dir")
    run.add_argument("--no-judge", action="store_true")
    run.add_argument("--offline-only", action="store_true")
    run.add_argument(
        "--target-model",
        default="",
        help="被测模型（默认取界面「用例与模型」里的值，再退到全局模型配置）",
    )
    run.add_argument(
        "--judge-model",
        default="",
        help="裁判模型（默认取界面里的值，再退到 DEVOPS_AGENT_JUDGE_MODEL / DEVOPS_AGENT_MODEL）",
    )
    run.set_defaults(func=lambda args: asyncio.run(_run(args)))

    regrade = sub.add_parser("grade", help="用已有轨迹重判")
    regrade.add_argument("--traces", required=True)
    regrade.add_argument("--cases")
    regrade.add_argument("--plan")
    regrade.add_argument("--out-dir")
    regrade.add_argument(
        "--db", help="评测库（读取裁判校准档位；默认 workspace/evals/openjudge.db）"
    )
    regrade.add_argument("--no-judge", action="store_true")
    regrade.add_argument("--judge-model", default="", help="裁判模型（默认取界面里的值）")
    regrade.set_defaults(func=lambda args: asyncio.run(_grade(args)))

    compare = sub.add_parser("compare", help="比较两个批次")
    compare.add_argument("--base", type=int, required=True)
    compare.add_argument("--head", type=int, required=True)
    compare.add_argument("--db")
    compare.set_defaults(func=_compare)

    calibration = sub.add_parser("calibration", help="导入裁判校准记录")
    calibration.add_argument("action", choices=["import"])
    calibration.add_argument("--file", required=True, help="校准记录 JSON，可为数组")
    calibration.set_defaults(func=_calibration)

    rubric = sub.add_parser("rubric", help="生成、校验或冻结 Rubric")
    rubric.add_argument("action", choices=["generate", "verify", "approve"])
    rubric.add_argument("--name", default="repo_qa_quality")
    rubric.add_argument("--version", default="v1")
    rubric.add_argument("--reviewer", default="")
    rubric.add_argument(
        "--task", default="基于真实文件回答，给出路径:行号，找不到就明说，不得编造。"
    )
    rubric.add_argument("--scenario", default="仓库结构/函数/配置问答，回答直接用于排障。")
    rubric.add_argument("--samples")
    rubric.set_defaults(func=lambda args: asyncio.run(_rubric(args)))

    memory = sub.add_parser("memory", help="记忆效果评测（SPEC-0014）")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_run = memory_sub.add_parser("run", help="跑确定性记忆批次（无需模型 Key）")
    memory_run.add_argument("--plan", default="eval/memory/plan.yaml")
    memory_run.add_argument(
        "--suite",
        default="must_pass",
        choices=["must_pass", "regression", "exploratory", ""],
    )
    memory_run.add_argument(
        "--variant",
        action="append",
        help="只跑指定变体（可重复；baseline/primary 会自动补上）",
    )
    memory_run.add_argument("--db")
    memory_run.set_defaults(func=lambda args: asyncio.run(_memory_run(args)))
    memory_list = memory_sub.add_parser("list", help="列出记忆批次")
    memory_list.add_argument("--limit", type=int, default=20)
    memory_list.add_argument("--db")
    memory_list.set_defaults(func=_memory_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
