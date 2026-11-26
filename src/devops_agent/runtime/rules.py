"""规则沉淀 —— 把「一次确认」翻译成「一条 allow 规则」，以及它的作用域校验。

三个决定，原料全部来自 config/policy.yaml 的 `confirm.rules`：

1. 哪些作用域可用（once / exact / prefix），顺序即由窄到宽；
2. `prefix` 落到哪一层（子命令程序取第二个词；python 取 `-m` 后的模块）；
3. 哪些情况**根本不允许沉淀**（复合命令、包装层剥不干净、危险程序、解释器）。

为什么第 3 条这么严：沉淀出一条 `rm:*` 或 `bash:*` 等于开了任意执行的门，
而「沉淀时的那次高危判定」管不到以后 —— 用户点的是「这条命令」，不是「这一整类」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from devops_agent.config import RulePolicy, load_policy
from devops_agent.runtime import shell
from devops_agent.runtime.risk import classify, command_of

# 默认作用域：配置缺失时的回落值（正常路径由 rule_scopes() 读配置）
_FALLBACK_SCOPES: Final = ("once", "exact", "prefix")


@dataclass(frozen=True, slots=True)
class ConfirmDecision:
    """前端回的一条确认结果。"""

    tool_call_id: str
    confirmed: bool
    scope: str = "once"


def _policy() -> RulePolicy:
    """取当前的沉淀策略（load_policy 已缓存，这里不做二次缓存）。"""
    return load_policy().confirm.rules


def rule_scopes() -> tuple[str, ...]:
    """本部署可用的作用域，顺序由配置给出（前端按它渲染选项）。"""
    scopes = tuple(_policy().scopes) or _FALLBACK_SCOPES
    return scopes


def _python_prefix(program: str, argv: list[str], python_programs: list[str]) -> str | None:
    """`-m` 后的模块算子命令；`-c` 是任意代码，不给前缀沉淀。"""
    if program not in python_programs:
        return program
    if len(argv) < 2:
        return program
    if argv[1] == "-c":
        return None
    if argv[1] == "-m" and len(argv) > 2:
        return f"{program} -m {argv[2]}"
    return program


def _prefix_of_program(program: str, argv: list[str], policy: RulePolicy) -> str | None:
    """按程序类型决定前缀深度：子命令程序取第二个词，其余只取程序名。"""
    if program in policy.python_programs:
        return _python_prefix(program, argv, policy.python_programs)
    if program in policy.subcommand_programs:
        # 跳过开关，取第一个「子命令」：`git commit -m x` → `git commit`
        rest = [arg for arg in argv[1:] if not arg.startswith("-")]
        return f"{program} {rest[0]}" if rest else program
    return program


def prefix_for(command: str) -> str | None:
    """单段命令 → 规则前缀（不含 `:*`）；拆不出来就返回 None。"""
    policy = _policy()
    if not command or shell.is_compound(command, policy.separators):
        return None
    argv = shell.split_argv(command)
    if argv is None:
        return None
    argv = shell.strip_wrappers(argv, policy.wrappers)
    if not argv:
        return None
    program = shell.basename(argv[0])
    if program in policy.never_persist or program in policy.never_persist_interpreters:
        return None
    return _prefix_of_program(program, argv, policy)


def rule_content_for(scope: str, name: str, raw_input: str) -> str | None:
    """按作用域生成规则内容。

    - once：不沉淀规则
    - exact：完整命令，最窄
    - prefix：命令前缀，框架按前缀匹配
    """
    command = command_of(name, raw_input).strip()
    if scope == "exact":
        return command or None
    if scope == "prefix":
        prefix = prefix_for(command)
        return f"{prefix}:*" if prefix else None
    return None


def parse_decisions(payload: list[dict[str, Any]]) -> list[ConfirmDecision]:
    """校验并归一前端提交的确认结果。

    非法 scope 退回 once（最窄），不做「按最宽解释」的兜底 ——
    宁可多问一次，也不要因为一个错拼的作用域把整类命令永久放行。
    """
    allowed = set(rule_scopes())
    decisions: list[ConfirmDecision] = []
    for item in payload:
        call_id = str(item.get("tool_call_id", "")).strip()
        if not call_id:
            continue
        scope = str(item.get("scope", "once"))
        decisions.append(
            ConfirmDecision(
                tool_call_id=call_id,
                confirmed=bool(item.get("confirmed", False)),
                scope=scope if scope in allowed else "once",
            )
        )
    return decisions


def effective_scope(name: str, raw_input: str, scope: str) -> str:
    """高危命令只允许「这一次」：一秒钟的勾选不该变成永久放行 `rm -rf`。"""
    if classify(name, raw_input) == "danger":
        return "once"
    return scope if scope in set(rule_scopes()) else "once"
