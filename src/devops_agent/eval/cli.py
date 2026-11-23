"""评测闭环 CLI：validate / run / compare。

`python -m devops_agent.eval validate --suite eval/suites/devops-core.v1.jsonl`
是 CI 的质量门禁；`run` 才会真正调用模型，需要模型凭据与数据库写权限。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devops_agent.env import load_env
from devops_agent.eval import report as report_mod
from devops_agent.eval.dataset import (
    EvalCase,
    Suite,
    load_suite,
    split_warnings,
    validate_suite,
)
from devops_agent.eval.judge import JudgeError, OpenAIJudge
from devops_agent.eval.runner import DEFAULT_FIXTURE_ROOT, CaseRun, case_run_payload, run_suite
from devops_agent.observability import repository as repo
from devops_agent.runtime.agent import PROMPT_ID, prompt_fingerprint


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _print_errors(messages: Sequence[str]) -> None:
    for message in messages:
        stream = sys.stdout if message.startswith("warning:") else sys.stderr
        print(message, file=stream)


def cmd_validate(args: argparse.Namespace) -> int:
    """校验数据集；有错误退出 1，warning 不阻断。"""
    suite = load_suite(args.suite)
    messages = validate_suite(suite)
    _print_errors(messages)
    errors, _warnings = split_warnings(messages)
    if errors:
        return 1
    print(f"ok {len(suite.cases)} cases")
    return 0


def _fixture_root_for(suite_path: str) -> Path:
    """suite 在 `eval/suites/` 时，fixtures 默认在同级 `eval/fixtures`。"""
    candidate = Path(suite_path).resolve().parent.parent / "fixtures"
    return candidate if candidate.is_dir() else DEFAULT_FIXTURE_ROOT


def _runtime_factory(profile_override: str | None) -> Callable[[EvalCase, Path], Any]:
    """延迟 import 真实运行时：只做 validate 时不必加载 agentscope。"""
    from devops_agent.governance.profiles import resolve
    from devops_agent.runtime.agent import build_agentscope_runtime

    def make(case: EvalCase, workdir: Path) -> Any:
        profile = resolve(profile_override or case.permission)
        return build_agentscope_runtime(
            workdir=workdir,
            profile=profile,
            web_search=case.web_search,
        )

    return make


def _persist(
    db_path: Path,
    suite: Suite,
    summary: dict[str, Any],
    runs: list[CaseRun],
    profile: str,
) -> int:
    """把批次与逐用例结果写入 SQLite。"""
    started_at = _now()
    with repo.connect(db_path) as conn:
        repo.init_db(conn)
        batch_id = repo.create_eval_batch(
            conn,
            started_at=started_at,
            suite=suite.manifest.suite,
            suite_version=suite.manifest.version,
            suite_sha=suite.manifest.sha256,
            prompt_id=PROMPT_ID,
            prompt_sha=prompt_fingerprint(),
            model=os.environ.get("DEVOPS_AGENT_MODEL", ""),
            profile=profile,
        )
        for run in runs:
            payload = case_run_payload(run)
            repo.insert_eval_case(
                conn,
                batch_id=batch_id,
                case_id=run.case.id,
                status=run.trace.status,
                passed=run.score.passed,
                scores_json=json.dumps(run.judge_scores or {}, ensure_ascii=False),
                detail_json=json.dumps(payload, ensure_ascii=False),
                latency_ms=run.latency_ms,
                tokens=run.tokens,
            )
        repo.finish_eval_batch(
            conn,
            batch_id,
            status="completed",
            summary_json=json.dumps(summary, ensure_ascii=False),
            finished_at=_now(),
        )
    return batch_id


def _write_outputs(out_dir: Path, summary: dict[str, Any], runs: list[CaseRun]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out_dir / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for run in runs:
            handle.write(json.dumps(case_run_payload(run), ensure_ascii=False) + "\n")
    (out_dir / "report.md").write_text(report_mod.render_markdown(summary), encoding="utf-8")


def _print_summary(summary: dict[str, Any], batch_id: int) -> None:
    print(
        f"batch={batch_id} n={summary['n']} passed={summary['passed']} "
        f"pass_rate={summary['pass_rate']:.2%} verdict={summary['verdict']}"
    )
    hard = summary.get("hard_failures", {})
    print(f"hard_failures={hard}")
    print(f"trajectory={summary.get('trajectory_score')} p95={summary.get('p95_ms')}ms")


def cmd_run(args: argparse.Namespace) -> int:
    """真实运行一套用例并落库、写报告。"""
    load_env()
    if not os.environ.get("DEVOPS_AGENT_API_KEY"):
        print(
            "错误: 缺少 DEVOPS_AGENT_API_KEY，无法运行真实模型评测。"
            "请先在 .env 或环境变量中配置模型凭据。",
            file=sys.stderr,
        )
        return 2

    suite = load_suite(args.suite)
    judge = None
    if args.judge:
        try:
            judge = OpenAIJudge()
        except JudgeError as exc:
            print(f"错误: 裁判初始化失败: {exc}", file=sys.stderr)
            return 2

    profile = args.profile or "per-case"
    try:
        runs = asyncio.run(
            run_suite(
                suite,
                _runtime_factory(args.profile),
                fixture_root=_fixture_root_for(args.suite),
                judge=judge,
                limit=args.limit,
            )
        )
    except Exception as exc:  # 运行时装配失败（凭据/档位/工作区）需要可读错误
        print(f"错误: 评测执行失败: {exc}", file=sys.stderr)
        return 2

    summary = report_mod.aggregate(runs, suite.manifest.min_cases_for_gate)
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"错误: 读取基线失败: {exc}", file=sys.stderr)
            return 2
        compared = report_mod.compare(summary, baseline)
        summary["baseline_id"] = baseline.get("id")
        summary["verdict"] = compared["verdict"]
        summary["compare"] = compared

    try:
        batch_id = _persist(
            Path(args.db) if args.db else repo.eval_db_path(),
            suite,
            summary,
            runs,
            profile,
        )
    except (sqlite3.Error, OSError) as exc:
        print(f"错误: 写入评测数据库失败: {exc}", file=sys.stderr)
        return 2

    _write_outputs(Path(args.out), summary, runs)
    _print_summary(summary, batch_id)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """比较两份 report.json；fail/warn/insufficient 均返回非零。"""
    try:
        current = json.loads(Path(args.report).read_text(encoding="utf-8"))
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"错误: 读取报告失败: {exc}", file=sys.stderr)
        return 2
    compared = report_mod.compare(current, baseline)
    for metric in compared["metrics"]:
        print(
            f"{metric['key']}: base={metric['base']} head={metric['head']} "
            f"delta={metric['delta']} better={metric['better']}"
        )
    changed = [item for item in compared["cases"] if item["base_passed"] != item["head_passed"]]
    print(f"cases_changed={len(changed)} verdict={compared['verdict']}")
    return 0 if compared["verdict"] == "pass" else 1


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（供测试与 __main__ 共用）。"""
    parser = argparse.ArgumentParser(prog="python -m devops_agent.eval")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验数据集")
    validate.add_argument("--suite", required=True)
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="真实运行评测")
    run.add_argument("--suite", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--judge", action="store_true")
    run.add_argument("--profile", default=None)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--baseline", default=None)
    run.add_argument("--db", default=None)
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="与基线对比")
    compare.add_argument("--report", required=True)
    compare.add_argument("--baseline", required=True)
    compare.set_defaults(func=cmd_compare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    return int(args.func(args))
