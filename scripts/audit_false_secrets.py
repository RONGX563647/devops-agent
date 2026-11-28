"""审计输出侧误捕获的「假密钥」，默认只读。

背景：旧的高熵兜底把 Python 标识符当成凭据登记进保险箱（`source=output`），
同一批规则还把它们替换进了模型上下文。规则已修（SPEC-0015 §3），但库里
已经躺着这些条目 —— 它们会让占位符注入把标识符当明文回填。

用法：

    python scripts/audit_false_secrets.py           # 只列可疑条目，不删
    python scripts/audit_false_secrets.py --purge   # 逐条确认后删除

判定：解密后按现行形状闸复核，`source=output` 且不再像凭据的条目即为可疑。
删除是不可逆的，所以默认 dry-run；`--purge` 也要求逐条键入 y 确认，
输入 `all` 才批量删除。
"""

from __future__ import annotations

import argparse
import sys

from devops_agent.redaction import rules
from devops_agent.secrets.vault import get_vault


def suspicious(names_and_values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """筛出「形态上不再是凭据」的输出侧条目。"""
    return [
        (name, value)
        for name, value in names_and_values
        if not rules.shape_ok(value, rules.CONFIDENCE_ASSIGNED)
    ]


def _collect() -> list[tuple[str, str, str]]:
    vault = get_vault()
    rows: list[tuple[str, str, str]] = []
    for meta in vault.list():
        if meta.source != "output":
            continue
        value = vault.get(meta.name)
        if value is not None:
            rows.append((meta.name, value, meta.created_at))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="列出（可选清理）输出侧误捕获的假密钥")
    parser.add_argument("--purge", action="store_true", help="确认后删除可疑条目")
    args = parser.parse_args(argv)

    rows = _collect()
    suspects = suspicious([(name, value) for name, value, _ in rows])
    if not suspects:
        print(f"检查 {len(rows)} 条 output 条目，未发现可疑项。")
        return 0

    print(f"检查 {len(rows)} 条 output 条目，{len(suspects)} 条可疑：")
    for name, value in suspects:
        preview = value if len(value) <= 40 else value[:40] + "…"
        print(f"  {name}  len={len(value)}  {preview!r}")

    if not args.purge:
        print("\n只读模式。确认无误后加 --purge 逐条删除。")
        return 0

    vault = get_vault()
    purge_all = False
    for name, _value in suspects:
        if not purge_all:
            answer = input(f"删除 {name}？[y/N/all] ").strip().lower()
            if answer == "all":
                purge_all = True
            elif answer != "y":
                continue
        vault.delete(name)
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
