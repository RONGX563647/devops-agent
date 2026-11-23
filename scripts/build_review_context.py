#!/usr/bin/env python3
"""把 PR diff 与直接调用方代码打包成审查上下文（纯 stdlib）。

控制上移：由确定性脚本决定模型看什么，而不是让 agent 自己多轮探索。
用法：python scripts/build_review_context.py <base-sha>
"""

from __future__ import annotations

import re
import subprocess
import sys

MAX_DIFF_LINES = 800
MAX_CONTEXT_CHARS = 12000
CALLER_WINDOW = 15
MAX_SYMBOLS = 10


def git(*args: str) -> str:
    """执行 git 命令并返回 stdout；失败时返回空串（如 git grep 无匹配返回 1）。"""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def changed_symbols(diff: str) -> list[str]:
    """从新增行里提取 def/class 名，作为找调用方的线索。"""
    found = (
        m.group(1)
        for line in diff.splitlines()
        if line.startswith("+") and (m := re.search(r"\b(?:def|class)\s+(\w+)", line))
    )
    return list(dict.fromkeys(found))[:MAX_SYMBOLS]


def caller_snippets(symbols: list[str], changed_files: set[str]) -> str:
    """在 src/ 里找符号引用点，截取上下文；跳过本次改动的文件。"""
    blocks: list[str] = []
    for symbol in symbols:
        hits = git("grep", "-n", symbol, "--", "src").splitlines()[:3]
        for hit in hits:
            parts = hit.split(":", 2)
            if len(parts) < 2:
                continue
            path, lineno, *_ = parts
            if path in changed_files:
                continue
            lines = git("show", f"HEAD:{path}").splitlines()
            start, end = max(0, int(lineno) - CALLER_WINDOW - 1), int(lineno) + CALLER_WINDOW
            blocks.append(f"[{path}:{lineno}]\n" + "\n".join(lines[start:end]))
            if sum(len(b) for b in blocks) > MAX_CONTEXT_CHARS:
                return "\n\n".join(blocks)
    return "\n\n".join(blocks)


def build(base: str) -> str:
    """构造完整审查上下文文本。"""
    diff = "\n".join(git("diff", base, "HEAD", "--", "src", "tests").splitlines()[:MAX_DIFF_LINES])
    changed = {line[4:] for line in diff.splitlines() if line.startswith("+++ b/")}
    callers = caller_snippets(changed_symbols(diff), changed)
    return f"=== DIFF ===\n{diff}\n\n=== CALLER CONTEXT ===\n{callers}"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: build_review_context.py <base-sha>")
    print(build(sys.argv[1]))


if __name__ == "__main__":
    main()
