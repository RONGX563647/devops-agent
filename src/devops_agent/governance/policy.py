"""治理 —— 用内置权限引擎，不手搓权限矩阵。

PermissionMode: default / accept_edits / explore / bypass / dont_ask
PermissionContext(mode, working_directories, allow_rules, deny_rules, ask_rules)
PermissionRule(tool_name, rule_content, behavior, source)
"""

from __future__ import annotations

from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)

from devops_agent.config import RuleSpec, RuntimePolicy, load_policy


def deny_rule(tool_name: str, source: str = "policy") -> PermissionRule:
    """构造一条拒绝规则（如禁用写工具）。"""
    return PermissionRule(
        tool_name=tool_name,
        rule_content=None,
        behavior=PermissionBehavior.DENY,
        source=source,
    )


def _to_rule_map(specs: list[RuleSpec]) -> dict[str, list[PermissionRule]]:
    """把声明式规则按工具名分桶，转成 AgentScope 规则对象。"""
    buckets: dict[str, list[PermissionRule]] = {}
    for spec in specs:
        buckets.setdefault(spec.tool_name, []).append(
            PermissionRule(
                tool_name=spec.tool_name,
                rule_content=spec.rule_content,
                behavior=PermissionBehavior(spec.behavior),
                source=spec.source,
            )
        )
    return buckets


def build_permission_context(
    mode: PermissionMode | None = None,
    deny_rules: dict[str, list[PermissionRule]] | None = None,
    *,
    policy: RuntimePolicy | None = None,
) -> PermissionContext:
    """按模式装配权限上下文；省略参数时从 config/policy.yaml 读取。

    单独暴露 context 而不只给 engine：Agent 的权限判定读的是
    `AgentState.permission_context`（Agent 内部自建引擎），装配时直接注入
    context 才能让 policy.yaml 真正生效，无需另建一套引擎。
    """
    resolved = policy if policy is not None else load_policy()
    cfg = resolved.permission
    return PermissionContext(
        mode=mode if mode is not None else PermissionMode(cfg.mode),
        working_directories={
            w.path: AdditionalWorkingDirectory(path=w.path, source=w.source)
            for w in cfg.working_directories
        },
        allow_rules=_to_rule_map(cfg.allow_rules),
        deny_rules=deny_rules if deny_rules is not None else _to_rule_map(cfg.deny_rules),
        ask_rules=_to_rule_map(cfg.ask_rules),
    )


def build_permission_engine(
    mode: PermissionMode | None = None,
    deny_rules: dict[str, list[PermissionRule]] | None = None,
    *,
    policy: RuntimePolicy | None = None,
) -> PermissionEngine:
    """按模式装配权限引擎；省略参数时从 config/policy.yaml 读取。"""
    return PermissionEngine(build_permission_context(mode, deny_rules, policy=policy))
