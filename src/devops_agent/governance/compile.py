"""把档位与规则编译成框架的 `PermissionContext` —— 本包唯一 import agentscope 的模块。

单独暴露 context 而不只给 engine：Agent 的权限判定读的是
`AgentState.permission_context`（Agent 内部自建引擎），装配时直接注入 context
才能让规则真正生效，不需要另建一套引擎。`build_engine` 只是给脚本/评测用的
便利包装。
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)

from devops_agent.governance.model import (
    ALLOW_PREFIXES,
    BUILTIN_DENY,
    SCOPE_ORDER,
    PermissionProfile,
    Rule,
)

# 附加工作目录：原来写在 policy.yaml 的 `permission.working_directories`（永远是空列表）
WORKDIRS_ENV = "DEVOPS_AGENT_WORKDIRS"


def _to_rule_map(rules: Iterable[Rule]) -> dict[str, list[PermissionRule]]:
    """按工具名分桶，转成框架规则对象。"""
    buckets: dict[str, list[PermissionRule]] = {}
    for rule in rules:
        buckets.setdefault(rule.tool, []).append(
            PermissionRule(
                tool_name=rule.tool,
                rule_content=rule.pattern,
                behavior=PermissionBehavior(rule.behavior),
                source=rule.source,
            )
        )
    return buckets


def _merge_deny_rules(
    builtin: dict[str, list[PermissionRule]],
    caller: Mapping[str, Sequence[PermissionRule]] | None,
) -> dict[str, list[PermissionRule]]:
    """caller 的 deny 追加到内置集合上，而不是整体替换。

    不能直接 `{**builtin, **caller}`：内置 deny 分散在 Bash/Write/Edit 三个
    tool_name 下，按 tool_name 覆盖会让 caller 传一条 Bash 规则就抹掉 `.git/`、
    `.env` 这两条点防御。判重键是 (tool, content, behavior)：`pattern=None`（整工具）
    与空串（同样匹配一切）是两种声明，不能互相顶掉。
    """
    if not caller:
        return builtin
    merged = {tool: list(rules) for tool, rules in builtin.items()}
    for tool, rules in caller.items():
        overridden = {(rule.tool_name, rule.rule_content, rule.behavior) for rule in rules}
        kept = [
            rule
            for rule in merged.get(tool, [])
            if (rule.tool_name, rule.rule_content, rule.behavior) not in overridden
        ]
        merged[tool] = kept + list(rules)
    return merged


def _active_rules(profile: PermissionProfile) -> tuple[Rule, ...]:
    """按生效下限过滤：`scope=container` 的规则只在放开档装配。

    同一条 `rm -rf` 在 workspace 档必须留 ASK（框架的 safety-ask 会问人），在
    bypass 档必须 DENY（那一档按契约跳过 safety-ask，Ask 不会有人回答）。
    """
    ceiling = SCOPE_ORDER[profile.scope]
    return tuple(rule for rule in BUILTIN_DENY if SCOPE_ORDER[rule.scope] <= ceiling)


def _allow_rules(profile: PermissionProfile) -> tuple[Rule, ...]:
    """档位的放行规则：只有 execute 档带命令白名单，其余档位为空。"""
    return tuple(
        Rule(tool="Bash", pattern=prefix, behavior="allow", source="profile")
        for prefix in ALLOW_PREFIXES.get(profile.key, ())
    )


def _container_path(raw: str) -> str | None:
    """是「当前平台当不了宿主绝对路径」的 POSIX 路径就原样返回，否则返回 None。

    宿主路径与容器路径不能共用一套规范化：

    - 宿主路径要 `expanduser()` + `resolve()`，否则 `~/x`、符号链接都跟工具给的
      路径比不相等；
    - 容器路径（docker 模式的 `/workspace`）在 Windows 上 `resolve()` 会绑到
      「当前盘符的根」：同一份部署从 `C:` 启动登记 `C:\\workspace`、从 `E:` 启动
      登记 `E:\\workspace`。容器里根本没有这个路径，判定结果随启动目录漂移。

    判据刻意收得很窄 —— 只有「当前平台不认为它是绝对路径」时才按容器路径处理：
    POSIX 上 `/workspace` 是合法的宿主绝对路径，照旧 `resolve()`，行为不变；
    只有 Windows 上它没有盘符（`is_absolute()` 为 False），才必须原样保留。
    """
    if not raw.startswith("/") or raw.startswith("//"):
        return None  # `//server/share` 在 Windows 上是 UNC 宿主路径
    if Path(raw).is_absolute():
        return None  # POSIX：宿主绝对路径，照常 expanduser + resolve
    return posixpath.normpath(raw)


def _registered(raw: str, source: str) -> tuple[str, AdditionalWorkingDirectory]:
    """把一个路径声明转成 `(键, 条目)`，按它是容器路径还是宿主路径分别规范化。"""
    text = raw.strip()
    path = _container_path(text)
    if path is None:
        path = str(Path(text).expanduser().resolve())
    return path, AdditionalWorkingDirectory(path=path, source=source)


def _working_dirs(workdir: str | Path | None) -> dict[str, AdditionalWorkingDirectory]:
    """env 声明的附加工作目录 + 本次 workdir。

    workdir 一定要登记：框架判定「这个路径在不在工作区内」用的是
    `os.getcwd() + context.working_directories`，而部署时真正的工作区常常不是
    进程 cwd（`DEVOPS_AGENT_WORKDIR` 指到别处）。不登记的话，`accept_edits` 档
    写文件会被判成「工作区外」→ 每次都要确认。

    docker 模式下这里收到的是容器路径 `/workspace`，登记也必须原样是它 ——
    工具在容器里报的路径就是 `/workspace/...`。
    """
    dirs: dict[str, AdditionalWorkingDirectory] = {}
    for raw in os.environ.get(WORKDIRS_ENV, "").split(os.pathsep):
        if not raw.strip():
            continue
        path, entry = _registered(raw, "env")
        dirs[path] = entry
    if workdir is not None:
        path, entry = _registered(str(workdir), "workdir")
        dirs.setdefault(path, entry)
    return dirs


def compile_context(
    profile: PermissionProfile,
    *,
    workdir: str | Path | None = None,
    extra_deny: Mapping[str, Sequence[PermissionRule]] | None = None,
) -> PermissionContext:
    """按档位装配权限上下文；builtin deny 无条件并入。"""
    return PermissionContext(
        mode=PermissionMode(profile.mode),
        working_directories=_working_dirs(workdir),
        allow_rules=_to_rule_map(_allow_rules(profile)),
        deny_rules=_merge_deny_rules(_to_rule_map(_active_rules(profile)), extra_deny),
        ask_rules={},
    )


def build_engine(
    profile: PermissionProfile, *, workdir: str | Path | None = None
) -> PermissionEngine:
    """按档位装配权限引擎（脚本 / 评测用；产品路径注入 context）。"""
    return PermissionEngine(compile_context(profile, workdir=workdir))
