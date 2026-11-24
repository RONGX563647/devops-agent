"""确认协议 —— 框架无关的数据结构与风险分级。

这一层不 import agentscope：确认事件的对象构造放在 runtime/agent.py（装配点），
HTTP 层只跟这里的数据结构打交道。

一条重要约定：**客户端永远不回传命令原文**。
`ToolConfirmResultEvent` 需要的是完整 `ToolCallBlock`，如果让前端把它带回来，
就等于允许客户端改写将要执行的命令。所以服务端保留权威副本，
客户端只回 `tool_call_id + confirmed + scope`，规则也由服务端按原始命令生成。
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 传给前端的命令原文上限：只用于展示，超长命令截断后由界面折叠
DISPLAY_LIMIT = 2000

# 明确的破坏性命令：确认卡片标红，并且不允许「本会话允许」
_DANGER = re.compile(
    r"(rm\s+-[a-zA-Z]*r[a-zA-Z]*f|rm\s+-[a-zA-Z]*f[a-zA-Z]*r|:\s*\(\s*\)\s*\{|mkfs|"
    r"dd\s+if=|shutdown|reboot|chmod\s+-R\s+0?777|>\s*/dev/sd)",
)
# 会落盘但不算破坏：标黄
_WRITE_HINTS = re.compile(r"(sed\s+-i|>>?|\btouch\b|\bmkdir\b|\brm\b|\bmv\b|\bcp\b|\btee\b)")

RISK_LABELS = {"read": "只读", "exec": "执行", "write": "写入", "danger": "高危"}

# 确认作用域：一次 / 精确这条命令 / 这个命令前缀
RULE_SCOPES = ("once", "exact", "prefix")


@dataclass(frozen=True, slots=True)
class PendingCall:
    """一条等待确认的调用（下发给前端展示用）。"""

    tool_call_id: str
    name: str
    input: str
    risk: str


@dataclass(frozen=True, slots=True)
class ConfirmDecision:
    """前端回的一条确认结果。"""

    tool_call_id: str
    confirmed: bool
    scope: str = "once"


def _parse(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def command_of(name: str, raw_input: str) -> str:
    """取这条调用真正要执行的命令 / 目标路径（用于展示与规则生成）。"""
    data = _parse(raw_input)
    for key in ("command", "file_path", "path", "pattern"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def classify(name: str, raw_input: str) -> str:
    """把一次调用分成 read / exec / write / danger 四档。"""
    if name in ("Write", "Edit"):
        return "write"
    if name not in ("Bash", "PowerShell"):
        return "read"
    command = command_of(name, raw_input)
    if _DANGER.search(command):
        return "danger"
    if _WRITE_HINTS.search(command):
        return "write"
    return "exec"


def display_input(name: str, raw_input: str) -> str:
    """下发给前端的展示文本：优先只给命令本身，别再套一层 JSON。"""
    command = command_of(name, raw_input)
    text = command or raw_input
    return text if len(text) <= DISPLAY_LIMIT else text[:DISPLAY_LIMIT] + "…"


def pending_call(block: dict[str, Any]) -> PendingCall:
    """从一个 ToolCallBlock 的 dict 形态生成展示项。"""
    name = str(block.get("name", ""))
    raw = str(block.get("input", "") or "")
    return PendingCall(
        tool_call_id=str(block.get("id", "")),
        name=name,
        input=display_input(name, raw),
        risk=classify(name, raw),
    )


def rule_content_for(scope: str, name: str, raw_input: str) -> str | None:
    """按作用域生成规则内容。

    - once：不沉淀规则
    - exact：完整命令，最窄
    - prefix：命令前缀，框架按前缀匹配

    prefix 必须落到「子命令」这一层，并且复合命令一律不沉淀：

    - `git commit -m x` 只取首词会得到 `git:*` —— 等于把整个 git 放行，
      用户点的是「这类命令」，不是「git 的全部子命令」；正确结果是 `git commit:*`。
    - `cd /repo && rm -rf build` 首词是 `cd`，沉淀成 `cd:*` 后，下一轮再出现
      同一段命令的最前半截就被放过了，而用户的意图显然不是「cd 随便用」。
      多段命令本来就该逐段判定，不做整体沉淀。
    """
    command = command_of(name, raw_input).strip()
    if scope == "exact":
        return command or None
    if scope == "prefix":
        prefix = _prefix_for(command)
        return f"{prefix}:*" if prefix else None
    return None


# 需要看「子命令」的程序：只放行到第二个词，避免一次点击放行整个工具。
_SUBCOMMAND_PROGRAMS = frozenset(
    {
        "git",
        "gh",
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "pip",
        "uv",
        "poetry",
        "cargo",
        "go",
        "docker",
        "docker-compose",
        "kubectl",
        "helm",
        "terraform",
        "make",
        "dotnet",
        "mvn",
        "gradle",
        "apt",
        "apt-get",
        "brew",
        "systemctl",
        "journalctl",
        "az",
        "aws",
        "gcloud",
        "conda",
        "alembic",
    }
)

# 复合命令的分隔符：命中任意一个就不沉淀（见上面 docstring 的第二个例子）
_SEPARATORS = ("&&", "||", ";", "|", "\n")

# 包装层：`timeout 30 npm test` 直接取首词会沉淀成 `timeout:*` —— 比不沉淀更糟，
# 那等于放行所有被 timeout 包住的命令。剥掉后落到真正的程序上。
_WRAPPERS = frozenset({"timeout", "time", "nice", "nohup", "stdbuf", "env", "command", "exec"})

# 一旦被沉淀成前缀就等于开了一扇任意执行的门，这些程序永不做前缀沉淀：
# `rm:*` 会连 `rm -rf /` 一起放过（沉淀时的那次高危判定管不到以后），
# `bash:*` / `python -c` 更是直接给了任意代码执行。
_NEVER_PERSIST = frozenset(
    {"rm", "rmdir", "mkfs", "dd", "shutdown", "reboot", "chmod", "chown", "sudo", "su"}
)
_NEVER_PERSIST_INTERPRETERS = frozenset({"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"})


def _basename(program: str) -> str:
    return program.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _is_compound(command: str) -> bool:
    """命令里有没有 shell 分隔符（引号与转义内的不算）。"""
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\":
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if any(command.startswith(sep, index) for sep in _SEPARATORS):
            return True
        index += 1
    return False


def _drop_leading_flags(args: list[str]) -> list[str]:
    """只剥包装器自己的开头的开关，剥到第一个非开关为止。

    不能把所有以 `-` 开头的词都删掉：`nohup python -m pytest -q` 里的 `-m`
    属于被包装的命令，删了就变成 `python:*`（放行一切 python 脚本）。
    """
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 1
    return args[index:]


_DURATION = re.compile(r"[\d.]+[smhd]?")
_ADJUSTMENT = re.compile(r"-?\d+")
# timeout 里带取值的开关：取值不带 `-`，不跳过会挡住后面的时长解析
_TIMEOUT_VALUE_FLAGS = ("-s", "--signal", "-k", "--kill-after")
PYTHON_PROGRAMS = frozenset({"python", "python3", "py"})


def _strip_timeout(rest: list[str]) -> list[str]:
    """跳过 `-s SIG` / `-k 5` / 开关 / 时长，留下真正被执行的命令。"""
    while rest and rest[0] in _TIMEOUT_VALUE_FLAGS:
        rest = rest[2:]
    rest = _drop_leading_flags(rest)
    if rest and _DURATION.fullmatch(rest[0]):
        rest = rest[1:]
    return rest


def _strip_nice(rest: list[str]) -> list[str]:
    """nice -n 5 cmd：`-n` 之后那个裸数字是调整值，不是程序。"""
    rest = _drop_leading_flags(rest)
    if rest and _ADJUSTMENT.fullmatch(rest[0]):
        rest = rest[1:]
    return rest


def _strip_env(rest: list[str]) -> list[str]:
    """env VAR=1 cmd：跳过一串变量赋值。"""
    rest = _drop_leading_flags(rest)
    while rest and "=" in rest[0]:
        rest = rest[1:]
    return rest


# 每类包装层一个剥离函数；没单列的（time / nohup / stdbuf / command / exec）
# 只需要去掉自己开头的开关。
_STRIPPERS: dict[str, Callable[[list[str]], list[str]]] = {
    "timeout": _strip_timeout,
    "nice": _strip_nice,
    "env": _strip_env,
}


def _strip_wrappers(argv: list[str]) -> list[str]:
    """剥掉 timeout / time / nice / nohup / stdbuf / env 等包装层。

    不剥的话 `timeout 30 npm test` 会沉淀成 `timeout:*` —— 一条放行所有
    「被 timeout 包住的命令」的规则，比不沉淀更糟。
    """
    args = list(argv)
    while args and _basename(args[0]) in _WRAPPERS:
        strip = _STRIPPERS.get(_basename(args[0]), _drop_leading_flags)
        args = strip(args[1:])
    return args


def _python_prefix(program: str, argv: list[str]) -> str | None:
    """`-m` 后的模块算子命令；`-c` 是任意代码，不给前缀沉淀。"""
    if len(argv) < 2:
        return program
    if argv[1] == "-c":
        return None
    if argv[1] == "-m" and len(argv) > 2:
        return f"{program} -m {argv[2]}"
    return program


def _prefix_of_program(program: str, argv: list[str]) -> str | None:
    if program in PYTHON_PROGRAMS:
        return _python_prefix(program, argv)
    if program in _SUBCOMMAND_PROGRAMS:
        rest = [arg for arg in argv[1:] if not arg.startswith("-")]
        return f"{program} {rest[0]}" if rest else program
    return program


def _prefix_for(command: str) -> str | None:
    """单段命令 → 规则前缀（不含 `:*`）；拆不出来就返回 None。"""
    if not command or _is_compound(command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:  # 引号不闭合：宁可不沉淀，也不要生成一条乱匹配的规则
        return None
    argv = _strip_wrappers(argv)
    if not argv:
        return None
    program = _basename(argv[0])
    if program in _NEVER_PERSIST or program in _NEVER_PERSIST_INTERPRETERS:
        return None
    return _prefix_of_program(program, argv)


def parse_decisions(payload: list[dict[str, Any]]) -> list[ConfirmDecision]:
    """校验并归一前端提交的确认结果。

    非法 scope 退回 once（最窄），不做「按最宽解释」的兜底 ——
    宁可多问一次，也不要因为一个错拼的作用域把整类命令永久放行。
    """
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
                scope=scope if scope in RULE_SCOPES else "once",
            )
        )
    return decisions


def effective_scope(name: str, raw_input: str, scope: str) -> str:
    """高危命令只允许「这一次」：一秒钟的勾选不该变成永久放行 `rm -rf`。"""
    if classify(name, raw_input) == "danger":
        return "once"
    return scope if scope in RULE_SCOPES else "once"
