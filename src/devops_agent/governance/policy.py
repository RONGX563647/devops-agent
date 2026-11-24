"""治理 —— 用内置权限引擎，不手搓权限矩阵。

PermissionMode: default / accept_edits / explore / bypass / dont_ask
PermissionContext(mode, working_directories, allow_rules, deny_rules, ask_rules)
PermissionRule(tool_name, rule_content, behavior, source)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)
from agentscope.tool import Bash

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


def _profile_deny_specs(_profile: PermissionProfile, policy: RuntimePolicy) -> list[RuleSpec]:
    """档位的拒绝规则 = policy 声明的全部 deny，无条件装配。

    builtin deny（`.git/`、`.env` 等）是对**所有档位**的硬约束，不随档位开关。
    旧实现只在 hard_deny 档（unrestricted）装配 `source: profile` 的 deny，
    理由是「其余档位越界会 ASK」——但 `auto` 档的 dont_ask 对 `.env` 这类
    工作区内路径直接 allow，ASK 根本不会触发，等于无人值守档能读密钥文件。
    框架层 deny 优先于 allow/ask，所以无条件并入才是正确的安全默认。
    """
    return list(policy.permission.deny_rules)


def build_permission_context(
    mode: PermissionMode | None = None,
    deny_rules: dict[str, list[PermissionRule]] | None = None,
    *,
    profile: PermissionProfile | None = None,
    policy: RuntimePolicy | None = None,
    workdir: str | Path | None = None,
) -> PermissionContext:
    """按档位（或显式 mode）装配权限上下文；省略参数时从 config/policy.yaml 读取。

    单独暴露 context 而不只给 engine：Agent 的权限判定读的是
    `AgentState.permission_context`（Agent 内部自建引擎），装配时直接注入
    context 才能让 policy.yaml 真正生效，无需另建一套引擎。

    `workdir` 一定要传：框架判定「这个路径在不在工作区内」用的是
    `os.getcwd() + context.working_directories`（见 Write._path_in_allowed_working_path），
    而部署时真正的工作区常常不是进程 cwd（`DEVOPS_AGENT_WORKDIR` 指到别处）。
    不把它登记进来，`accept_edits` 档写文件会被判成「工作区外」→ 每次都要确认，
    用户看到的就是「明明选了工作区可写，写个文件还要问」。
    """
    resolved = policy if policy is not None else load_policy()
    cfg = resolved.permission
    working = _working_dirs(resolved)
    if workdir is not None:
        target = str(Path(workdir).expanduser().resolve())
        working.setdefault(target, AdditionalWorkingDirectory(path=target, source="workdir"))
    if profile is None:
        return PermissionContext(
            mode=mode if mode is not None else PermissionMode(cfg.mode),
            working_directories=working,
            allow_rules=_to_rule_map(cfg.allow_rules),
            deny_rules=deny_rules if deny_rules is not None else _to_rule_map(cfg.deny_rules),
            ask_rules=_to_rule_map(cfg.ask_rules),
        )
    return PermissionContext(
        mode=mode if mode is not None else PermissionMode(profile.mode),
        working_directories=working,
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


def bash_decision(profile: PermissionProfile, command: str, workdir: str | Path) -> str:
    """该档位对一条 bash 命令的判定：`allow` / `ask` / `deny`。

    这是「静态预演」而不是执行：给题库 lint 用，判断一条用例要求的命令
    在它声明的档位下是否真的可达。放在本模块是因为它是 `import agentscope`
    的边界之一——`eval/lint.py` 只通过这个函数间接使用框架。
    """
    engine = build_permission_engine(profile=profile)
    engine.context.working_directories.setdefault(
        str(Path(workdir).expanduser().resolve()),
        AdditionalWorkingDirectory(path=str(Path(workdir).expanduser().resolve()), source="lint"),
    )
    tool = Bash(cwd=str(workdir))
    decision = asyncio.run(engine.check_permission(tool, {"command": command}))
    return str(decision.behavior.value)
