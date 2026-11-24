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
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devops_agent.env import load_env
from devops_agent.eval import report as report_mod
from devops_agent.eval.dataset import (
    GRADER_VERSION,
    EvalCase,
    Suite,
    load_suite,
    manifest_path_for,
    scenario_of,
    split_warnings,
    suite_sha256,
    validate_suite,
)
from devops_agent.eval.judge import JudgeError, OpenAIJudge, resolve_judge_model
from devops_agent.eval.lint import Finding, run_lint, split_findings
from devops_agent.eval.runner import DEFAULT_FIXTURE_ROOT, CaseRun, case_run_payload, run_suite
from devops_agent.observability import repository as repo
from devops_agent.runtime.agent import PROMPT_ID, prompt_fingerprint

# 这些 verdict 必须让 CI 变红。`insufficient` 不在其中：题库没到门禁样本量是正常
# 状态，天天报红只会让人习惯性忽略门禁。
_BLOCKING_VERDICTS: frozenset[str] = frozenset({"fail", "warn"})


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


def _load_known_issues(path: str | None) -> set[tuple[str, str]]:
    """读取「已接受的静态检查违规」清单：`{rule, where}` 二元组集合。

    存在的理由：新规则一定会先在**存量**题库上报警（这正是它的价值），
    但 CI 不能因此长期变红。把已知违规显式列出来，既能立刻启用规则挡住新增，
    又不必为了绿灯而关掉规则。

    清单项必须真实存在——对不上的条目会让命令失败，避免债务清单变成永久豁免。
    """
    if not path:
        return set()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("accepted", []) if isinstance(raw, dict) else raw
    known: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rule = str(row.get("rule") or "").strip()
        where = str(row.get("where") or "").strip()
        if rule and where:
            known.add((rule, where))
    return known


def _apply_known_issues(
    errors: Sequence[Finding], known: set[tuple[str, str]]
) -> tuple[list[Finding], list[Finding], list[tuple[str, str]]]:
    """把已知违规从 error 里摘出来。

    返回 `(仍阻断的 error, 已接受的 error, 失效清单项)`。
    失效清单项指：清单里有、但这次没再出现的条目——说明它被修好了，
    应该从清单里删掉，否则豁免会一直留着。
    """
    if not known:
        return list(errors), [], []
    blocking: list[Finding] = []
    accepted: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for item in errors:
        key = (item.rule, item.where)
        if key in known:
            accepted.append(item)
            seen.add(key)
        else:
            blocking.append(item)
    stale = sorted(known - seen)
    return blocking, accepted, stale


def _print_findings(findings: Sequence[Finding]) -> None:
    """error 走 stderr、warning 走 stdout，方便 CI 日志里一眼分开。"""
    for item in findings:
        stream = sys.stderr if item.level == "error" else sys.stdout
        print(f"[{item.level}] {item.rule} {item.where}: {item.message}", file=stream)
        if item.hint:
            print(f"         hint: {item.hint}", file=stream)


def cmd_prompt_lint(args: argparse.Namespace) -> int:
    """静态契约检查：不跑模型，查提示词 / 档位 / 题库是否自洽。"""
    suite = load_suite(args.suite) if args.suite else None
    fixture_root = _fixture_root_for(args.suite) if args.suite else None
    findings = run_lint(suite, fixture_root)
    errors, warnings = split_findings(findings)
    known = _load_known_issues(args.known_issues)
    blocking, accepted, stale = _apply_known_issues(errors, known)
    if args.json:
        payload = [
            {
                "rule": item.rule,
                "level": item.level,
                "where": item.where,
                "message": item.message,
                "hint": item.hint,
            }
            for item in findings
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        # 未拦下的 error 与已接受的 error 都要走 stderr：前者才是阻断项，
        # 但后者的级别仍是 error，混进 stdout 会让「有 error 却 err 为空」。
        _print_findings([*blocking, *accepted])
        _print_findings(warnings)
        if accepted:
            print(f"已接受（见 {args.known_issues}）：{len(accepted)} 条，清理后请从清单删除对应项")
        if stale:
            for rule, where in stale:
                print(f"[stale] {rule} {where}: 已修好，请从清单移除", file=sys.stderr)
        print(f"findings: {len(blocking)} error / {len(warnings)} warning")
    if blocking or stale:
        return 1
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    """回写清单：重算 sha256 / counts，并记录当前判卷口径。

    加字段就会改变 `suite_sha256()`，不提供回写入口的话，改一次题库就卡在
    「sha 不匹配」上无法继续。
    """
    suite = load_suite(args.suite)
    counts = Counter(
        scenario for case in suite.cases if (scenario := scenario_of(case)) is not None
    )
    manifest = suite.manifest.model_copy(
        update={
            "sha256": suite_sha256(suite.cases),
            "counts": dict(sorted(counts.items())),
            "grader_version": GRADER_VERSION,
        }
    )
    payload = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if args.dry_run:
        print(payload)
        return 0
    target = manifest_path_for(Path(args.suite))
    target.write_text(payload + "\n", encoding="utf-8")
    print(f"updated {target}（{len(suite.cases)} cases, grader {GRADER_VERSION}）")
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
    gates = summary.get("gates")
    if isinstance(gates, list):
        for gate in gates:
            if isinstance(gate, dict):
                print(
                    f"gate {gate.get('key')}: {gate.get('value')} "
                    f"(min {gate.get('min')}, samples {gate.get('samples')}) "
                    f"→ {gate.get('state')}"
                )
    print(f"trajectory={summary.get('trajectory_score')} p95={summary.get('p95_ms')}ms")


def _thresholds(suite: Suite) -> dict[str, dict[str, float]]:
    """把 manifest 的阈值声明转成 aggregate 需要的形状（单一来源在 manifest）。"""
    return {
        key: {"min": spec.min, "min_samples": spec.min_samples}
        for key, spec in suite.manifest.thresholds.items()
    }


def _db_path(override: str | None) -> Path:
    return Path(override) if override else repo.eval_db_path()


def _init_judge(enabled: bool) -> tuple[OpenAIJudge | None, str]:
    """按需装配裁判；返回 `(judge, error)`，error 为空串代表成功。"""
    if not enabled:
        return None, ""
    try:
        return OpenAIJudge(), ""
    except JudgeError as exc:
        return None, f"错误: 裁判初始化失败: {exc}"


def _factors(
    suite: Suite, profile: str, judge_enabled: bool, judge_scope: str = "all"
) -> dict[str, Any]:
    """写进报告的实验因子。没有它，compare 无法判断两批是不是同一个实验。

    `judge_model` 必须与裁判实际使用的模型一致：它是可比性检查的阻断因子，
    记录错了会让「换了裁判模型」被判成同一实验（delta 被误当成结论）。
    """
    model = os.environ.get("DEVOPS_AGENT_MODEL", "")
    return {
        "suite_sha": suite.manifest.sha256,
        "grader_version": GRADER_VERSION,
        "prompt_sha": prompt_fingerprint(),
        "model": model,
        "profile": profile,
        "judge_model": resolve_judge_model() if judge_enabled else "",
        # 裁判覆盖范围会直接改变 judge 均值，两批不同就不可比
        "judge_scope": judge_scope if judge_enabled else "",
    }


def _apply_baseline(summary: dict[str, Any], baseline_path: str) -> str:
    """把与基线的对比结论写回 summary；返回错误信息（空串代表成功）。"""
    try:
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"错误: 读取基线失败: {exc}"
    compared = report_mod.compare(summary, baseline)
    summary["baseline_id"] = baseline.get("id")
    summary["verdict"] = compared["verdict"]
    summary["compare"] = compared
    return ""


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
    judge, judge_error = _init_judge(args.judge)
    if judge_error:
        print(judge_error, file=sys.stderr)
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
                judge_scope=args.judge_scope,
            )
        )
    except Exception as exc:  # 运行时装配失败（凭据/档位/工作区）需要可读错误
        print(f"错误: 评测执行失败: {exc}", file=sys.stderr)
        return 2

    summary = report_mod.aggregate(
        runs,
        suite.manifest.min_cases_for_gate,
        thresholds=_thresholds(suite),
    )
    summary.update(_factors(suite, profile, args.judge, args.judge_scope))
    if args.baseline:
        baseline_error = _apply_baseline(summary, args.baseline)
        if baseline_error:
            print(baseline_error, file=sys.stderr)
            return 2

    try:
        batch_id = _persist(
            _db_path(args.db),
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
    # 退出码语义：
    # - `fail`：硬失败或门禁未达 —— 必须红；
    # - `warn`：与基线比有退化（超过精度上限）—— 同样必须红，否则「-20pp 回归
    #   只报个警告」会被习惯性忽略；
    # - `insufficient`：题库没到门禁样本量，属正常状态，不红；
    # - `pass`：绿。
    #
    # `--allow-warn` 只给手动排查用，nightly 不传。
    if summary["verdict"] in _BLOCKING_VERDICTS and not args.allow_warn:
        return 1
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
    compat = compared.get("compatibility") or {}
    print(
        f"compatibility={compat.get('level')} "
        f"changed={','.join(compat.get('changed') or []) or '-'}"
    )
    print(f"mde={compared.get('mde')}（小于该值的变化视为抖动）")
    print(f"cases_changed={len(changed)} verdict={compared['verdict']}")
    return 0 if compared["verdict"] == "pass" else 1


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（供测试与 __main__ 共用）。"""
    parser = argparse.ArgumentParser(prog="python -m devops_agent.eval")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验数据集")
    validate.add_argument("--suite", required=True)
    validate.set_defaults(func=cmd_validate)

    lint = sub.add_parser("prompt-lint", help="静态检查提示词/档位/题库是否自洽")
    lint.add_argument("--suite", default=None, help="不传则只查提示词与档位")
    lint.add_argument("--json", action="store_true")
    lint.add_argument(
        "--known-issues",
        default=None,
        help="已接受的存量违规清单（JSON）；清单里没有的违规仍然阻断",
    )
    lint.set_defaults(func=cmd_prompt_lint)

    manifest = sub.add_parser("manifest", help="重算清单的 sha256 / counts")
    manifest.add_argument("--suite", required=True)
    manifest.add_argument("--dry-run", action="store_true")
    manifest.set_defaults(func=cmd_manifest)

    run = sub.add_parser("run", help="真实运行评测")
    run.add_argument("--suite", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--judge", action="store_true")
    # 默认 all：只在规则通过的用例上打分会让难用例退出分母，
    # 退化时 judge 均值反而上升。passed 仅用于省额度。
    run.add_argument(
        "--judge-scope",
        choices=("all", "passed"),
        default="all",
        help="裁判覆盖范围：all=所有配了裁判的用例（默认，无偏）；passed=仅规则通过的用例（省额度但有偏）",
    )
    run.add_argument("--profile", default=None)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--baseline", default=None)
    run.add_argument("--db", default=None)
    # 手动排查用：让 warn 也退 0。CI / nightly 一律不传。
    run.add_argument(
        "--allow-warn",
        action="store_true",
        help="warn 也视为通过（仅手动排查；CI 不要用）",
    )
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
