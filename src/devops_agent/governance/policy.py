"""治理 —— 用内置权限引擎，不手搓权限矩阵。

职责只有一件：把「档位 + 规则」编译成框架的 `PermissionContext`。
判定永远交给 `PermissionEngine`，本模块不写任何 if-else 判定逻辑 —— 手搓
出来的矩阵一旦与框架顺序（deny → ask → allow）不一致，就会出现「点了允许还是问」
这种谁都查不出来的问题。

六段管线（真正执行判定的只有框架的三个桶 + mode）：

| 层 | 内容 | 落在哪 |
|---|---|---|
| ① | deny · builtin（`.git/` `.env` `.ssh/` `.aws/` `rm -rf /`） | `deny_rules` |
| ② | deny · project / user | `deny_rules` |
| ③ | ask · 显式高危（`rm:*`、`git push --force:*` …） | `ask_rules` |
| ④ | allow · 精确 | `allow_rules` |
| ⑤ | allow · 前缀（`pytest:*`） | `allow_rules` |
| ⑥ | 档位默认 | `mode`，**不是规则** |

⑥ 必须是 mode：框架顺序是 ask 先于 allow（实测），把「非白名单命令都问」写成
`ask(Bash:*)` 就会排在任何 allow 前面，用户点过的「以后都这样」全部失效 ——
这正是「切换档位后还是一直 ask」的机制性原因。
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

from devops_agent.config import RuntimePolicy, load_policy
from devops_agent.governance import store
from devops_agent.governance.profiles import PermissionProfile
from devops_agent.governance.rules import Rule, buckets, builtin_rules, merge_rules


def deny_rule(tool_name: str, source: str = "policy") -> PermissionRule:
    """构造一条拒绝规则（如禁用写工具）。"""
    return PermissionRule(
        tool_name=tool_name,
        rule_content=None,
        behavior=PermissionBehavior.DENY,
        source=source,
    )


def _to_rule_map(rules: list[Rule]) -> dict[str, list[PermissionRule]]:
    """按工具名分桶，把框架无关的 `Rule` 转成 `PermissionRule`。"""
    grouped: dict[str, list[PermissionRule]] = {}
    for rule in rules:
        grouped.setdefault(rule.tool, []).append(
            PermissionRule(
                tool_name=rule.tool,
                rule_content=rule.pattern,
                behavior=PermissionBehavior(rule.behavior),
                source=rule.source,
            )
        )
    return grouped


def _working_dirs(cfg: RuntimePolicy) -> dict[str, AdditionalWorkingDirectory]:
    """policy.yaml 声明的附加工作目录 → 框架结构。"""
    return {
        w.path: AdditionalWorkingDirectory(path=w.path, source=w.source)
        for w in cfg.permission.working_directories
    }


def collect_rules(policy: RuntimePolicy | None = None) -> list[Rule]:
    """① ② ③ ⑤ 层 + 本地沉淀规则，去重后合并。

    顺序即优先级：本地规则能盖过 `policy.yaml`，内置规则盖过所有 —— 内置 deny
    是底线，不该被一条本地 allow 覆盖掉。`command_allowlist` 在这里变成
    `source=project` 的 allow 规则，**不再是某个档位的属性**：「白名单命令不问」
    是规则层的事，与「打扰程度」无关。
    """
    resolved = policy if policy is not None else load_policy()
    cfg = resolved.permission
    declared = [
        Rule(
            tool=spec.tool_name,
            pattern=spec.rule_content,
            behavior=spec.behavior,
            source=spec.source,
            scope=spec.scope,
        )
        for spec in (*cfg.allow_rules, *cfg.ask_rules, *cfg.deny_rules)
    ]
    # 白名单从 workspace 起生效：只读档谈「放行执行」没有意义，而且 explore
    # 模式本来就忽略 allow 规则。
    allowlist = [
        Rule(
            tool="Bash",
            pattern=prefix,
            behavior="allow",
            source="project",
            scope="workspace",
            origin="command_allowlist",
        )
        for prefix in cfg.command_allowlist
    ]
    return merge_rules(declared, allowlist, store.local_rules(), builtin_rules())


def compile_context(
    profile: PermissionProfile,
    rules: list[Rule] | None = None,
    *,
    policy: RuntimePolicy | None = None,
) -> PermissionContext:
    """档位 + 规则 → 框架权限上下文。三名单 + mode 是唯一出口。"""
    resolved = policy if policy is not None else load_policy()
    compiled = rules if rules is not None else collect_rules(resolved)
    parts = buckets(compiled, profile.scope, profile.approvals)
    if profile.mode == "explore":
        # explore 没有「先问再放行」的通道：问下去只会把会话悬在一个没人能批的
        # 状态（服务端没有确认入口时会静默中断）。只读档的语义是「越界即拒」，
        # 所以显式 ask 在这里降级为 deny —— 与 ADR-0002 的判定矩阵一致。
        parts["deny"] = [*parts["deny"], *parts["ask"]]
        parts["ask"] = []
    return PermissionContext(
        mode=PermissionMode(profile.mode),
        working_directories=_working_dirs(resolved),
        allow_rules=_to_rule_map(parts["allow"]),
        deny_rules=_to_rule_map(parts["deny"]),
        ask_rules=_to_rule_map(parts["ask"]),
    )


def _with_overrides(
    context: PermissionContext,
    mode: PermissionMode | None,
    deny_rules: dict[str, list[PermissionRule]] | None,
) -> PermissionContext:
    """把调用方显式给的 mode / deny_rules 盖到已编译的上下文上。"""
    updates: dict[str, object] = {}
    if mode is not None:
        updates["mode"] = mode
    if deny_rules is not None:
        updates["deny_rules"] = deny_rules
    return context.model_copy(update=updates) if updates else context


def _context_from_yaml(
    mode: PermissionMode | None,
    deny_rules: dict[str, list[PermissionRule]] | None,
    policy: RuntimePolicy,
) -> PermissionContext:
    """没有档位时：policy.yaml 里写了什么就用什么（脚本 / 单测直接建引擎的路径）。

    内置 deny 仍然并入 —— 它是底线，不随调用方式变化。
    """
    declared = collect_rules(policy)
    return PermissionContext(
        mode=mode if mode is not None else PermissionMode(policy.permission.mode),
        working_directories=_working_dirs(policy),
        allow_rules=_to_rule_map([r for r in declared if r.behavior == "allow"]),
        deny_rules=(
            deny_rules
            if deny_rules is not None
            else _to_rule_map([r for r in declared if r.behavior == "deny"])
        ),
        ask_rules=_to_rule_map([r for r in declared if r.behavior == "ask"]),
    )


def build_permission_context(
    mode: PermissionMode | None = None,
    deny_rules: dict[str, list[PermissionRule]] | None = None,
    *,
    profile: PermissionProfile | None = None,
    policy: RuntimePolicy | None = None,
) -> PermissionContext:
    """按档位装配权限上下文；`profile` 省略时保持旧的「只用 policy.yaml」行为。

    单独暴露 context 而不只给 engine：Agent 的权限判定读的是
    `AgentState.permission_context`（Agent 内部自建引擎），装配时直接注入
    context 才能让 policy.yaml 真正生效，无需另建一套引擎。
    """
    if profile is not None:
        return _with_overrides(compile_context(profile, policy=policy), mode, deny_rules)
    return _context_from_yaml(mode, deny_rules, policy if policy is not None else load_policy())


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


# 前端「不问 / 会问」清单的取样命令。刻意覆盖四类：只读、白名单、git、高危。
# 清单必须由真实引擎跑出来，不能手写文案 —— 手写的「越界才问」正是上一版
# 与实际判定对不上的原因。
PREVIEW_COMMANDS: tuple[str, ...] = (
    "ls -la",
    "cat src/main.py",
    "git status",
    "pytest -q",
    "npm test",
    "git commit -m x",
    "mkdir build",
    "rm -rf build",
    "cat .env",
)


async def describe(
    profile: PermissionProfile, rules: list[Rule] | None = None
) -> dict[str, list[str]]:
    """用真实引擎跑一遍取样命令，得出这一档「不问什么 / 会问什么 / 什么被拒」。"""
    # 延迟导入：本模块是框架边界，但没必要为了预览在导入期就构造工具对象
    from agentscope.tool import Bash, Write

    engine = PermissionEngine(compile_context(profile, rules))
    bash = Bash(cwd=".")
    write = Write()
    result: dict[str, list[str]] = {"allow": [], "ask": [], "deny": []}
    for command in PREVIEW_COMMANDS:
        decision = await engine.check_permission(bash, {"command": command})
        result[decision.behavior.value].append(command)
    decision = await engine.check_permission(write, {"file_path": "a.py", "content": "x"})
    result[decision.behavior.value].append("写文件")
    return result
