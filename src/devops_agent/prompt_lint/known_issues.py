"""「已接受的静态检查违规」清单的读写与分流。

存在的理由：新规则一定会先在**存量**上报警（这正是它的价值），但 CI 不能因此
长期变红。把已知违规显式列出来，既能立刻启用规则挡住新增，又不必为了绿灯而
关掉规则。清单项必须真实存在 —— 对不上的条目会以 stale 报出来，避免债务清单
变成永久豁免。

迁移自旧 `eval/known_issues.py`，只换了 finding 类型的来源。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from devops_agent.prompt_lint.rules import Finding


def load_known_issues(path: str | None) -> set[tuple[str, str]]:
    """读取「已接受的静态检查违规」清单：`{rule, where}` 二元组集合。"""
    if not path:
        return set()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("accepted", []) if isinstance(raw, dict) else raw
    known: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rule = str(row.get("rule") or "").strip()
        where = str(row.get("where") or "").strip()
        if rule and where:
            known.add((rule, where))
    return known


def apply_known_issues(
    errors: Sequence[Finding], known: set[tuple[str, str]]
) -> tuple[list[Finding], list[Finding], list[tuple[str, str]]]:
    """把已知违规从 error 里摘出来。

    返回 `(仍阻断的 error, 已接受的 error, 失效清单项)`。
    失效清单项指：清单里有、但这次没再出现的条目 —— 说明它被修好了，
    应该从清单里删掉，否则豁免会一直留着。
    """
    if not known:
        return list(errors), [], []
    blocking: list[Finding] = []
    accepted: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for item in errors:
        key = (item.rule, item.where)
        if key in known:
            accepted.append(item)
            seen.add(key)
        else:
            blocking.append(item)
    stale = sorted(known - seen)
    return blocking, accepted, stale


def stale_findings(stale: Sequence[tuple[str, str]]) -> list[Finding]:
    """失效清单项转成 error 级 finding：豁免已经没用了，该删。

    级别定为 error 而不是 warning，是为了让旧 `eval lint` 的退出码语义
    （`blocking or stale` → 1）原样保留：豁免修好却不删清单，CI 必须变红，
    否则白名单会一点点长成永久豁免。
    """
    return [
        Finding(
            rule="baseline",
            level="error",
            where=f"{rule} {where}",
            message="白名单项已不再命中，请从清单删除",
            hint="豁免长期滞留会让新违规被静默接受",
        )
        for rule, where in stale
    ]


def print_findings(findings: Sequence[Finding]) -> None:
    """error 走 stderr、warning 走 stdout，方便 CI 日志里一眼分开。"""
    for item in findings:
        stream = sys.stderr if item.level == "error" else sys.stdout
        print(f"[{item.level}] {item.rule} {item.where}: {item.message}", file=stream)
        if item.hint:
            print(f"         hint: {item.hint}", file=stream)
