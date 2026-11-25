"""提示词静态契约检查（P1–P4）：工具闭包 / 占位符 / 写能力承诺 / 题面重复。

与旧 `eval/lint.py` 的区别：只保留与题库断言模型无关的规则，断言质量交给
`openjudge.benchmark.validate`。CLI 见 `python -m devops_agent.prompt_lint`。
"""

from __future__ import annotations

from devops_agent.prompt_lint.known_issues import (
    apply_known_issues,
    load_known_issues,
    print_findings,
    stale_findings,
)
from devops_agent.prompt_lint.rules import (
    Finding,
    check_duplicate_prompts,
    check_placeholders,
    check_tool_closure,
    check_write_claims,
    normalize_prompt,
    run_lint,
    split_findings,
)

__all__ = [
    "Finding",
    "apply_known_issues",
    "check_duplicate_prompts",
    "check_placeholders",
    "check_tool_closure",
    "check_write_claims",
    "load_known_issues",
    "normalize_prompt",
    "print_findings",
    "run_lint",
    "split_findings",
    "stale_findings",
]
