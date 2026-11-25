"""`python -m devops_agent.prompt_lint` —— 提示词静态契约检查的命令行入口。

```
python -m devops_agent.prompt_lint [--json] [--known-issues eval/lint-baseline.json]
                                   [--cases eval/openjudge/cases]
```

不跑模型、不联网，退出码由「白名单之外的 blocking」和「失效白名单项」决定
（与旧 `eval lint` 一致：修好的豁免必须从清单删掉，否则白名单会烂成永久豁免）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from devops_agent.governance.profiles import PROFILES
from devops_agent.openjudge.benchmark import Case, load_cases
from devops_agent.prompt_lint.known_issues import (
    apply_known_issues,
    load_known_issues,
    print_findings,
    stale_findings,
)
from devops_agent.prompt_lint.rules import Finding, run_lint, split_findings

# `prompt_lint/cli.py` -> prompt_lint -> devops_agent -> src -> 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES_DIR = _REPO_ROOT / "eval" / "openjudge" / "cases"
# 沿用旧 prompt-lint 的白名单文件：格式不变，存量本来就是空清单，
# 另起一份只会让同一类豁免散在两个文件里。
DEFAULT_KNOWN_ISSUES = _REPO_ROOT / "eval" / "lint-baseline.json"


def _case_paths(raw: str | None) -> list[Path]:
    """`--cases` 可以是目录或单个 jsonl；默认扫 openjudge 用例目录。"""
    if raw is None:
        return sorted(DEFAULT_CASES_DIR.glob("*.jsonl"))
    path = Path(raw)
    return sorted(path.glob("*.jsonl")) if path.is_dir() else [path]


def _load_cases(raw: str | None) -> list[Case]:
    paths = _case_paths(raw)
    if not paths:
        raise FileNotFoundError(f"没有找到用例文件: {raw or DEFAULT_CASES_DIR}")
    return load_cases(paths)


def _known_path(raw: str | None) -> str | None:
    """默认白名单文件不存在时当作空清单，不因缺文件报错。"""
    if raw:
        return raw
    return str(DEFAULT_KNOWN_ISSUES) if DEFAULT_KNOWN_ISSUES.is_file() else None


def _payload(findings: Sequence[Finding], blocking: int, advice: int, waived: int) -> str:
    rows = [
        {
            "rule": item.rule,
            "level": item.level,
            "where": item.where,
            "message": item.message,
            "hint": item.hint,
        }
        for item in findings
    ]
    body = {"findings": rows, "blocking": blocking, "advice": advice, "waived": waived}
    return json.dumps(body, ensure_ascii=False, indent=2)


def cmd_lint(args: argparse.Namespace) -> int:
    """跑全部规则，按白名单分流，输出 JSON 或文本。"""
    try:
        cases = _load_cases(args.cases)
        known = load_known_issues(_known_path(args.known_issues))
    except (OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    findings = run_lint(profiles=list(PROFILES.values()), cases=cases)
    errors, advice = split_findings(findings)
    blocking, waived, stale = apply_known_issues(errors, known)
    # 失效清单项并入 blocking：它说的是「这条豁免已经没用了」，不删就等于
    # 白名单在慢性腐烂。旧 CLI 也是 `if blocking or stale: return 1`，这里照抄。
    stale_items = stale_findings(stale)
    blocking = [*blocking, *stale_items]

    if args.json:
        print(_payload([*findings, *stale_items], len(blocking), len(advice), len(waived)))
    else:
        print_findings([*blocking, *waived])
        print_findings(advice)
        if waived:
            print(f"已接受（白名单）：{len(waived)} 条")
        print(f"findings: {len(blocking)} blocking / {len(advice)} advice")
    return 1 if blocking else 0


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（供测试与 `__main__` 共用）。"""
    parser = argparse.ArgumentParser(prog="python -m devops_agent.prompt_lint")
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    parser.add_argument(
        "--known-issues",
        default=None,
        help=f"已接受的存量违规清单（默认 {DEFAULT_KNOWN_ISSUES}，不存在则视为空）",
    )
    parser.add_argument(
        "--cases",
        default=None,
        help=f"openjudge 用例目录或单个 jsonl（默认 {DEFAULT_CASES_DIR}）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    return cmd_lint(args)
