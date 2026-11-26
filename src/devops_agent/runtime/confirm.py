"""确认协议 —— 对外门面（实现按职责拆在 risk / rules / shell 三个模块）。

这一层原来是一个 359 行文件，混了三件变更节奏完全不同的事：

| 子模块 | 职责 | 什么时候改 |
|---|---|---|
| `runtime/risk.py` | read / exec / write / danger 的判定与展示文本 | 安全口径变、标签文案变 |
| `runtime/rules.py` | 作用域、前缀沉淀、确认结果解析 | 「点一次能放行多少」变 |
| `runtime/shell.py` | 纯字符串层：拆段、剥包装层 | 拆词规则修 bug |

三条不变量仍然成立，且都在子模块里落实：

1. **客户端永远不回传命令原文**。`ConfirmDecision` 只带 `tool_call_id`，命令以服务端
   挂起态为权威（原文在 `runs.pending_json`），否则等于允许客户端改写将要执行的命令。
2. **高危命令不沉淀规则**。`effective_scope` 强制降级成 once —— 一秒钟的勾选不该
   变成永久放行 `rm -rf`。
3. **本层不 import agentscope**：确认协议要能用纯 dict 单测，框架对象只在装配点构造。

保留本模块作为门面，是为了让既有调用方（`scope_runtime` / `events` / `gateway/` /
`openjudge/`）不用改 import 路径，同时它本身就是「确认协议对外暴露什么」的清单。
新增能力请加在对应子模块，并在这里显式导出。
"""

from __future__ import annotations

from devops_agent.runtime.risk import (
    RISK_LABELS,
    PendingCall,
    classify,
    command_of,
    display_input,
    display_limit,
    pending_call,
)
from devops_agent.runtime.rules import (
    ConfirmDecision,
    effective_scope,
    parse_decisions,
    rule_content_for,
    rule_scopes,
)

__all__ = [
    "RISK_LABELS",
    "ConfirmDecision",
    "PendingCall",
    "classify",
    "command_of",
    "display_input",
    "display_limit",
    "effective_scope",
    "parse_decisions",
    "pending_call",
    "rule_content_for",
    "rule_scopes",
]
