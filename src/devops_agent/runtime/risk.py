"""风险分级 —— 一次工具调用属于 read / exec / write / danger 的哪一档。

判定规则是**域知识**，已外置到 config/policy.yaml 的 `confirm.risk`
（改错会直接改变「什么命令算高危」，所以它跟着权限策略走，并在加载期校验正则可编译）。
本模块只负责三件事：

1. 从工具入参里取出真正要执行的命令 / 目标路径；
2. 按「工具类型 → 正则」两段判定风险档；
3. 生成给前端展示的精简调用（含展示上限截断）。

不碰规则沉淀（见 runtime/rules.py），也不碰词法拆解（见 runtime/shell.py）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

from devops_agent.config import load_policy

# 风险标签：前端在确认卡片上按它染色。这是唯一一份中文标签，改文案只改这里。
RISK_LABELS: Final[dict[str, str]] = {
    "read": "只读",
    "exec": "执行",
    "write": "写入",
    "danger": "高危",
}

# 会落盘的工具：与 tools/catalog.py 的 WRITE_TOOL_NAMES 同义。
# 那边 import agentscope，而确认协议这一层刻意不引框架（要能用纯 dict 单测），
# 所以这里只留名字，由 tests/unit/test_confirm_protocol.py 的分类用例钉住。
WRITE_TOOL_NAMES: Final = ("Write", "Edit")
# 需要看命令内容才能判定的工具；其余工具一律按只读处理
SHELL_TOOL_NAMES: Final = ("Bash", "PowerShell")

# 命令里哪几个键承载「真正要执行的东西」：按优先级取第一个非空字符串
_COMMAND_KEYS: Final = ("command", "file_path", "path", "pattern")


@cache
def _alternation(patterns: tuple[str, ...]) -> re.Pattern[str]:
    """把多条正则合成一条（`(?:a)|(?:b)`），一次判定只跑一次匹配。

    cache 的键就是模式元组本身：配置变了必然产生新键，所以不需要额外的失效钩子
    （`config.clear_cache()` 清掉 load_policy 之后，这里自然跟着换）。
    """
    return re.compile("|".join(f"(?:{pattern})" for pattern in patterns))


def _matches(patterns: list[str], text: str) -> bool:
    """文本是否命中任意一条规则；空文本直接判否（避免 ``空正则`` 误伤）。"""
    if not patterns or not text:
        return False
    return bool(_alternation(tuple(patterns)).search(text))


def _parse(raw: str) -> dict[str, Any]:
    """把工具入参解析成 dict；非法 JSON 按空 dict 处理（照常判定，不抛）。"""
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def command_of(name: str, raw_input: str) -> str:
    """取这条调用真正要执行的命令 / 目标路径（用于展示与规则生成）。"""
    data = _parse(raw_input)
    for key in _COMMAND_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def classify(name: str, raw_input: str) -> str:
    """把一次调用分成 read / exec / write / danger 四档。

    顺序即优先级：写工具直接算 write（不看命令内容）；非 shell 工具算 read；
    shell 命令再按「高危 → 写入 → 执行」逐级判定。
    """
    if name in WRITE_TOOL_NAMES:
        return "write"
    if name not in SHELL_TOOL_NAMES:
        return "read"
    command = command_of(name, raw_input)
    risk = load_policy().confirm.risk
    if _matches(risk.danger_patterns, command):
        return "danger"
    if _matches(risk.write_patterns, command):
        return "write"
    return "exec"


def display_limit() -> int:
    """下发给前端的命令原文上限（超长只影响展示，不影响执行）。"""
    return load_policy().confirm.display_limit


def display_input(name: str, raw_input: str) -> str:
    """下发给前端的展示文本：优先只给命令本身，别再套一层 JSON。"""
    command = command_of(name, raw_input)
    text = command or raw_input
    limit = display_limit()
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(frozen=True, slots=True)
class PendingCall:
    """一条等待确认的调用（下发给前端展示用，不含可回传的完整块）。"""

    tool_call_id: str
    name: str
    input: str
    risk: str


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
