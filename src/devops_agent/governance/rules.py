"""权限规则 —— 框架无关的数据结构、内置规则与 shell 解析。

这一层刻意不 import agentscope（见 `AGENTS.md` 框架边界清单）：规则是纯数据，
求值交给框架的 `PermissionEngine`，编译在 `governance/policy.py` 完成。这样
「哪条命令会被拦」可以用纯结构体测试，不必先造一个 `Bash()` 再问引擎。

三个概念必须分清（混用会导致「设了没用」）：

- `scope`：能力范围下限。规则只在 `SCOPE_ORDER[当前档位] >= SCOPE_ORDER[规则]`
  时生效 —— 在 workspace 批准的 `pytest:*` 不该渗回 readonly。
- `approvals`：打扰程度。**只决定框架 mode**（兜底动作），不改规则集，也不改工具集。
- `behavior`：这条规则本身判 allow / ask / deny。求值顺序由框架固定为
  deny → ask → allow（实测，allow 盖不过 ask），所以「档位默认」绝不能写成
  ask 规则，否则用户沉淀的 allow 永远不生效。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SCOPES: tuple[str, ...] = ("readonly", "workspace", "container")
SCOPE_ORDER: dict[str, int] = {"readonly": 0, "workspace": 1, "container": 2}
BEHAVIORS: tuple[str, ...] = ("allow", "ask", "deny")
SOURCES: tuple[str, ...] = ("builtin", "project", "user", "session")

# shell 里会切断命令的分隔符。`&` 单独出现（后台执行）不切：`2>&1` 里也有 `&`，
# 切了会把重定向误判成两条命令。
_SEPARATORS: tuple[str, ...] = ("&&", "||", ";", "|", "\n")

# 只包一层、不改变实际执行程序的包装命令。剥掉之后才轮到「真正跑的是什么」。
_WRAPPERS: frozenset[str] = frozenset(
    {"timeout", "time", "nice", "nohup", "stdbuf", "env", "command", "exec"}
)

# 取「程序 + 子命令」而不是只取程序名：`git commit -m x` 沉淀成 `git:*` 等于
# 放行整个 git，`git add:*` 才是用户真正批准的那件事。
_SUBCOMMAND_PROGRAMS: frozenset[str] = frozenset(
    {"git", "npm", "npx", "pnpm", "yarn", "cargo", "go", "docker", "kubectl", "uv", "poetry"}
)

# 这些程序一旦出现在命令里就不该沉淀任何 allow —— 破坏性太强，谈不上「以后都这样」。
_NEVER_PERSIST: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "mkfs",
        "dd",
        "shutdown",
        "reboot",
        "chmod",
        "chown",
        "chattr",
        "mkswap",
        "sudo",
    }
)

# 危险的 git 子命令：`git push --force` / `git reset --hard` 会不可逆地改写远端或工作区。
_DANGER_PREFIXES: tuple[str, ...] = (
    "git push --force",
    "git push -f",
    "git reset --hard",
    "git clean -fdx",
)


@dataclass(frozen=True, slots=True)
class Rule:
    """一条权限规则。

    `pattern=None` 表示整个工具；`"pytest:*"` / `".env"` 由框架自己解释匹配方式
    （实测 Bash 规则按子命令前缀匹配，deny/ask 按子串匹配）。
    """

    tool: str
    pattern: str | None = None
    behavior: str = "ask"
    source: str = "builtin"
    scope: str = "readonly"
    # 生成这条规则的命令原文：审计与撤销时要知道「当初为什么放行」
    origin: str = ""
    created_at: str = ""

    @property
    def identity(self) -> tuple[str, str | None]:
        """去重键：同一个 (工具, 模式) 只保留一条，后写覆盖先写。"""
        return (self.tool, self.pattern)


def _rule(tool: str, pattern: str, behavior: str, *, origin: str = "") -> Rule:
    return Rule(tool=tool, pattern=pattern, behavior=behavior, source="builtin", origin=origin)


# ① 层：内置 deny。无条件装配 —— 之前只挂在 `hard_deny` 档，等于最需要防护的
# 档位最松。`bypass` 下框架仍然执行 deny（实测），所以这一层是真正的底线。
BUILTIN_DENY: tuple[Rule, ...] = (
    _rule("Bash", ".git/", "deny", origin="仓库元数据不可读"),
    _rule("Bash", ".env", "deny", origin="密钥文件不可读"),
    _rule("Bash", ".ssh/", "deny", origin="私钥目录不可读"),
    _rule("Bash", ".aws/", "deny", origin="云凭据目录不可读"),
    _rule("Bash", "rm -rf /", "deny", origin="根目录不可删"),
    _rule("Write", ".git/", "deny", origin="仓库元数据不可写"),
    _rule("Write", ".ssh/", "deny", origin="私钥目录不可写"),
    _rule("Write", ".aws/", "deny", origin="云凭据目录不可写"),
    _rule("Edit", ".git/", "deny", origin="仓库元数据不可写"),
    _rule("Edit", ".ssh/", "deny", origin="私钥目录不可写"),
    _rule("Edit", ".aws/", "deny", origin="云凭据目录不可写"),
)

# ③ 层：显式 ask（人写的「这个必须问我」）。框架顺序是 ask 先于 allow，
# 所以这一层不会被任何沉淀规则盖过 —— 这也是它只放高危命令的原因。
BUILTIN_ASK: tuple[Rule, ...] = (
    _rule("Bash", "rm:*", "ask", origin="删除不可逆"),
    _rule("Bash", "rmdir:*", "ask", origin="删除不可逆"),
    _rule("Bash", "mkfs:*", "ask", origin="格式化磁盘"),
    _rule("Bash", "dd:*", "ask", origin="裸设备写入"),
    _rule("Bash", "shutdown:*", "ask", origin="关机"),
    _rule("Bash", "reboot:*", "ask", origin="重启"),
    _rule("Bash", "chmod:*", "ask", origin="改权限位"),
    _rule("Bash", "chown:*", "ask", origin="改归属"),
    _rule("Bash", "git push --force:*", "ask", origin="改写远端历史"),
    _rule("Bash", "git reset --hard:*", "ask", origin="丢弃工作区改动"),
)


def builtin_rules() -> list[Rule]:
    """① 与 ③ 两层的内置规则。"""
    return [*BUILTIN_DENY, *BUILTIN_ASK]


def active_rules(rules: Iterable[Rule], scope: str) -> list[Rule]:
    """按能力范围过滤：规则只在「当前档位 >= 规则声明范围」时生效。"""
    rank = SCOPE_ORDER.get(scope, 0)
    return [rule for rule in rules if SCOPE_ORDER.get(rule.scope, 0) <= rank]


def buckets(rules: Iterable[Rule], scope: str, approvals: str) -> dict[str, list[Rule]]:
    """把规则编译成框架的三个桶。

    `approvals=always` 时把 allow 降级为 ask 而**不丢弃**：审计档要的是「每个动作
    都确认」，同时保留「这条命令之前被批准过」的信息，用户知道自己重复批准了什么。
    """
    active = active_rules(rules, scope)
    allow = [rule for rule in active if rule.behavior == "allow"]
    ask = [rule for rule in active if rule.behavior == "ask"]
    if approvals == "always":
        # 注意这里必须**只**在 always 时把 allow 挪进 ask：无条件加进去会让
        # 每条白名单同时命中 ask，ask 又先于 allow，结果就是「明明放行了还在问」
        allow, ask = (
            [],
            ask
            + [
                Rule(
                    tool=rule.tool,
                    pattern=rule.pattern,
                    behavior="ask",
                    source=rule.source,
                    scope=rule.scope,
                    origin=rule.origin,
                    created_at=rule.created_at,
                )
                for rule in allow
            ],
        )
    return {
        "deny": [rule for rule in active if rule.behavior == "deny"],
        "ask": ask,
        "allow": allow,
    }


def merge_rules(*groups: Iterable[Rule]) -> list[Rule]:
    """按 `identity` 去重合并；后出现的覆盖先出现的（本地规则能盖过内置）。

    内置 deny 不允许被覆盖 —— 那是底线，不是偏好。调用方应把内置组放在最后
    传入，或者用 `merge_rules` 之后再由 `buckets` 单独并入 `BUILTIN_DENY`。
    """
    merged: dict[tuple[str, str | None], Rule] = {}
    for group in groups:
        for rule in group:
            merged[rule.identity] = rule
    return list(merged.values())


def _separator_at(command: str, index: int) -> str | None:
    """当前位置是否是一个命令分隔符。"""
    for sep in _SEPARATORS:
        if command.startswith(sep, index):
            return sep
    return None


def _closing_quote(command: str, start: int) -> int:
    """返回闭合引号之后的下标；引号没有闭合就返回到末尾。

    整段引号内容一次性吞掉，所以主循环里不需要维护引号状态 ——
    `echo 'a && b'` 里的分隔符因此永远不会被当成命令边界。
    """
    quote = command[start]
    index = start + 1
    while index < len(command):
        if command[index] == "\\":
            index += 2
            continue
        if command[index] == quote:
            return index + 1
        index += 1
    return len(command)


def split_segments(command: str) -> list[str]:
    """按 `&&` / `||` / `;` / `|` / 换行切分，引号与转义内的分隔符不算。

    `cd /repo && rm -rf build` 切成两段之后，`rm` 那一段才可能被规则拦到 ——
    只取首词的话整条会被当成 `cd:*`，等于给破坏性命令发了通行证。
    """
    segments: list[str] = []
    buf: list[str] = []
    index = 0
    while index < len(command):
        char = command[index]
        if char in ("'", '"'):
            end = _closing_quote(command, index)
            buf.append(command[index:end])
            index = end
            continue
        if char == "\\":
            buf.append(command[index : index + 2])
            index += 2
            continue
        matched = _separator_at(command, index)
        if matched is not None:
            segments.append("".join(buf).strip())
            buf = []
            index += len(matched)
            continue
        buf.append(char)
        index += 1
    segments.append("".join(buf).strip())
    return [segment for segment in segments if segment]


def _strip_flags(args: Sequence[str], value_flags: frozenset[str] = frozenset()) -> list[str]:
    """丢掉主程序后面的选项，保留「真正的子命令」。"""
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            if arg in value_flags:
                skip = True
            continue
        out.append(arg)
    return out


def _strip_one(program: str, rest: list[str]) -> list[str]:
    """剥掉一层包装命令自己的参数，返回它真正要执行的部分。"""
    if program == "timeout":
        # timeout [-s SIG] DURATION cmd...
        rest = _strip_flags(rest, frozenset({"-s", "--signal"}))
        return rest[1:] if rest and re.fullmatch(r"[\d.]+[smhd]?", rest[0]) else rest
    if program == "nice":
        return _strip_flags(rest, frozenset({"-n", "--adjustment"}))
    if program in ("env", "stdbuf"):
        return [arg for arg in rest if not arg.startswith("-") and "=" not in arg]
    return _strip_flags(rest)


def strip_wrappers(argv: list[str]) -> list[str]:
    """剥掉 `timeout` / `time` / `nice` / `nohup` / `stdbuf` / `env` 等包装层。

    不剥的话 `timeout 30 npm test` 会沉淀成 `timeout:*` —— 一条匹配所有
    `timeout` 调用的规则，比不沉淀更糟。
    """
    args = list(argv)
    while args:
        program = _program_name(args[0])
        if program not in _WRAPPERS:
            return args
        args = _strip_one(program, args[1:])
    return args


def _program_name(token: str) -> str:
    """`/usr/bin/git` / `C:\\bin\\git.exe` → `git`（只用于比对包装器名单）。"""
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _is_dangerous(argv: list[str]) -> bool:
    joined = " ".join(argv)
    return any(joined.startswith(prefix) for prefix in _DANGER_PREFIXES)


def _program_prefix(program: str, argv: list[str]) -> str:
    """程序 + 子命令（能取到的话）。"""
    if program.startswith("python") and len(argv) > 2 and argv[1] in ("-m", "-c"):
        return f"{program} {argv[1]} {argv[2]}"
    if program in _SUBCOMMAND_PROGRAMS:
        rest = _strip_flags(argv[1:])
        return f"{program} {rest[0]}" if rest else program
    return program


def _prefix_of_segment(segment: str) -> str | None:
    """单段命令 → 规则前缀（不含 `:*`）；拆不出来就返回 None。"""
    try:
        argv = shlex.split(segment)
    except ValueError:  # 引号不闭合：宁可不让沉淀，也不要生成一条乱匹配的规则
        return None
    argv = strip_wrappers(argv)
    if not argv:
        return None
    program = _program_name(argv[0])
    if program in _NEVER_PERSIST or _is_dangerous(argv):
        return None
    return _program_prefix(program, argv)


def rule_prefix(command: str) -> str | None:
    """把一条命令折成规则的 prefix 作用域，如 `git commit:*`。

    多段命令一律返回 None：`cd /repo && rm -rf build` 只批准了 `cd`，却会顺带
    放行后半段 —— 「一条规则覆盖整条复合命令」本身就是错误的粒度。
    """
    segments = split_segments(command)
    if len(segments) != 1:
        return None
    prefix = _prefix_of_segment(segments[0])
    return f"{prefix}:*" if prefix else None


def rule_matches(rule_content: str | None, command: str) -> bool:
    """规则的粗粒度匹配（用于统计与提示，不参与实际判定）。

    实际判定永远交给框架引擎；这里只回答「这条规则大概管不管这条命令」，
    供「这条命令已经被问过 3 次」这类提示使用。
    """
    if rule_content is None:
        return True
    if rule_content.endswith(":*"):
        prefix = rule_content[:-2]
        return any(segment.startswith(prefix) for segment in split_segments(command))
    return rule_content in command
