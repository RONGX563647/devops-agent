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
from devops_agent.governance.profiles import PermissionProfile


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


def _working_dirs(cfg: RuntimePolicy) -> dict[str, AdditionalWorkingDirectory]:
    """policy.yaml 声明的附加工作目录 → 框架结构。"""
    return {
        w.path: AdditionalWorkingDirectory(path=w.path, source=w.source)
        for w in cfg.permission.working_directories
    }


def _profile_allow_specs(profile: PermissionProfile, policy: RuntimePolicy) -> list[RuleSpec]:
    """档位的放行规则 = policy 全局 allow_rules + （execute 档）命令白名单。"""
    specs = list(policy.permission.allow_rules)
    if profile.allowlist:
        specs.extend(
            RuleSpec(tool_name="Bash", rule_content=prefix, behavior="allow", source="profile")
            for prefix in policy.permission.command_allowlist
        )
    return specs


def _profile_deny_specs(profile: PermissionProfile, policy: RuntimePolicy) -> list[RuleSpec]:
    """档位的拒绝规则。

    `source: profile` 的规则只服务 hard_deny 档（unrestricted）—— 那一档跳过了
    逐次确认，deny_rules 是唯一剩下的约束；其余档位越界会 ASK，不需要这层。
    """
    hard = profile.fallback == "hard_deny"
    return [spec for spec in policy.permission.deny_rules if hard or spec.source != "profile"]


def build_permission_context(
    mode: PermissionMode | None = None,
    deny_rules: dict[str, list[PermissionRule]] | None = None,
    *,
    profile: PermissionProfile | None = None,
    policy: RuntimePolicy | None = None,
) -> PermissionContext:
    """按档位（或显式 mode）装配权限上下文；省略参数时从 config/policy.yaml 读取。

    单独暴露 context 而不只给 engine：Agent 的权限判定读的是
    `AgentState.permission_context`（Agent 内部自建引擎），装配时直接注入
    context 才能让 policy.yaml 真正生效，无需另建一套引擎。
    """
    resolved = policy if policy is not None else load_policy()
    cfg = resolved.permission
    if profile is None:
        return PermissionContext(
            mode=mode if mode is not None else PermissionMode(cfg.mode),
            working_directories=_working_dirs(resolved),
            allow_rules=_to_rule_map(cfg.allow_rules),
            deny_rules=deny_rules if deny_rules is not None else _to_rule_map(cfg.deny_rules),
            ask_rules=_to_rule_map(cfg.ask_rules),
        )
    return PermissionContext(
        mode=mode if mode is not None else PermissionMode(profile.mode),
        working_directories=_working_dirs(resolved),
        allow_rules=_to_rule_map(_profile_allow_specs(profile, resolved)),
        deny_rules=(
            deny_rules
            if deny_rules is not None
            else _to_rule_map(_profile_deny_specs(profile, resolved))
        ),
        ask_rules=_to_rule_map(cfg.ask_rules),
    )


def build_permission_engine(
    mode: PermissionMode | None = None,
    deny_rules: dict[str, list[PermissionRule]] | None = None,
    *,
    profile: PermissionProfile | None = None,
    policy: RuntimePolicy | None = None,
) -> PermissionEngine:
    """按档位装配权限引擎；省略参数时从 config/policy.yaml 读取。"""
    return PermissionEngine(
        build_permission_context(mode, deny_rules, profile=profile, policy=policy)
    )
