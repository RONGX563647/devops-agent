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
    - prefix：命令首个词 + ":*"，框架按前缀匹配
    """
    command = command_of(name, raw_input).strip()
    if scope == "exact":
        return command or None
    if scope == "prefix":
        head = command.split()[0] if command else ""
        return f"{head}:*" if head else None
    return None


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
