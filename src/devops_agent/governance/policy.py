"""治理 —— 用内置权限引擎，不手搓权限矩阵。

PermissionMode: default / accept_edits / explore / bypass / dont_ask
PermissionContext(mode, working_directories, allow_rules, deny_rules, ask_rules)
PermissionRule(tool_name, rule_content, behavior, source)
"""

from __future__ import annotations

from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)


def deny_rule(tool_name: str, source: str = "policy") -> PermissionRule:
    """构造一条拒绝规则（如禁用写工具）。"""
    return PermissionRule(
        tool_name=tool_name,
        rule_content=None,
        behavior=PermissionBehavior.DENY,
        source=source,
    )


def build_permission_engine(
    mode: PermissionMode = PermissionMode.DEFAULT,
    deny_rules: dict[str, list[PermissionRule]] | None = None,
) -> PermissionEngine:
    """按模式装配权限引擎。规则按工具名分桶。"""
    context = PermissionContext(mode=mode, deny_rules=deny_rules or {})
    return PermissionEngine(context)
