"""shell 命令词法 —— 只做字符串层面的拆解，不认识「权限」，也不读配置。

为什么独立成模块：这一层是纯函数（输入字符串、输出 argv / 布尔），是规则沉淀里
最容易写错、也最适合单测的部分。它原来埋在 `runtime/confirm.py` 里，与
「什么命令算高危」「哪条规则该沉淀」挤在同一个 359 行文件里 —— 改一个拆词规则
要先读完全部风险分级代码。

职责边界：
- 本模块只管「这段话怎么切成程序与参数」「有没有复合结构」；
- 哪些程序算包装层（timeout / env…）、哪些分隔符算复合，由调用方按配置传入
  （见 runtime/rules.py），所以这里不 import config，测试可以零配置直跑。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Collection, Sequence


def basename(program: str) -> str:
    """取可执行文件名：`/usr/bin/git` 与 `git` 归一到同一个键。"""
    return program.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _is_substitution(command: str, index: int, quote: str | None) -> bool:
    """当前位置是否是命令替换：`$(...)` 或反引号。

    双引号内仍会执行（`"$HOME"` 这类纯变量不会），单引号内才是字面量。
    别让 `echo "$(rm -rf /)"` 绕过拆段判定。
    """
    if quote == "'":
        return False
    return command.startswith("$(", index) or command[index] == "`"


def is_compound(command: str, separators: Collection[str]) -> bool:
    """命令里有没有 shell 分隔符或命令替换（字面量引号内的不算）。

    判为复合就不做前缀沉淀：`cd /repo && rm -rf build` 若沉淀成 `cd:*`，
    等于顺带放行了后面那半段 —— 多段命令本来就该逐段判定。
    """
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\":
            index += 2
            continue
        if quote is not None:
            if _is_substitution(command, index, quote):
                return True
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        # 命令替换里能藏任意命令，首词判定看不见内部：不拆段就会把
        # `echo $(rm -rf /)` 沉淀成 `echo:*` —— 等于放行所有 echo。
        if _is_substitution(command, index, quote) or any(
            command.startswith(sep, index) for sep in separators
        ):
            return True
        index += 1
    return False


def split_argv(command: str) -> list[str] | None:
    """把单段命令切成 argv；引号不闭合等拆不出来的情况返回 None。

    宁可不沉淀，也不要生成一条乱匹配的规则：`shlex` 抛错的输入无法保证后续
    前缀判定的语义。
    """
    if not command:
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


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
# 只需要去掉自己开头的开关。这张表是「结构知识」，不是部署策略，所以留在代码里。
_STRIPPERS: dict[str, Callable[[list[str]], list[str]]] = {
    "timeout": _strip_timeout,
    "nice": _strip_nice,
    "env": _strip_env,
}


def strip_wrappers(argv: Sequence[str], wrappers: Collection[str]) -> list[str]:
    """剥掉 timeout / time / nice / nohup / stdbuf / env 等包装层。

    不剥的话 `timeout 30 npm test` 会沉淀成 `timeout:*` —— 一条放行所有
    「被 timeout 包住的命令」的规则，比不沉淀更糟。
    """
    known = set(wrappers)
    args = list(argv)
    while args and basename(args[0]) in known:
        strip = _STRIPPERS.get(basename(args[0]), _drop_leading_flags)
        args = strip(args[1:])
    return args
