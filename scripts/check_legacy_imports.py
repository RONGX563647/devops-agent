"""禁止重新引入已删除的兼容门面。

门面删除后 import 会自然失败，但代码库仍可能被重新加回同名文件。
本脚本在 CI 中扫描源码与测试，阻止旧路径复活：

- Python：``devops_agent.main``、``devops_agent.runtime.agent``；
- 前端：``api/useRunStream``、``workspace/store``、``workspace/trace``、
  ``workspace/ModelConfigPopover``、``workspace/TraceList``、
  ``workspace/ChatTab``、``pages/EvalBatches``。

只扫描真正会参与构建的目录，历史文档不在范围内。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BACKEND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bdevops_agent\.main\b"),
        "devops_agent.main 已删除，请改为 devops_agent.gateway.*",
    ),
    (
        re.compile(r"\bdevops_agent\.runtime\.agent\b"),
        "devops_agent.runtime.agent 已删除，请改为 runtime.prompt/assembly 等",
    ),
    (
        re.compile(r"\bfrom\s+devops_agent\.runtime\s+import\s+agent\b"),
        "runtime.agent 已删除，请改为 runtime.prompt/assembly 等",
    ),
)

# 旧门面对应的 import 结尾片段；新路径不会命中（例如 store/state、traceData/*）。
FORBIDDEN_SPECIFIERS: dict[str, str] = {
    "api/useRunStream": "改用 api/stream",
    "workspace/store": "改用 workspace/store/{state,actions,history}",
    "store": "改用 workspace/store/{state,actions,history}",
    "workspace/trace": "改用 workspace/traceData/*",
    "trace": "改用 workspace/traceData/*",
    "workspace/ModelConfigPopover": "改用 workspace/model-config/*",
    "ModelConfigPopover": "改用 workspace/model-config/*",
    "workspace/TraceList": "改用 workspace/traceViews/TraceList",
    "TraceList": "改用 workspace/traceViews/TraceList",
    "workspace/ChatTab": "改用 workspace/chat/ChatTab",
    "ChatTab": "改用 workspace/chat/ChatTab",
    "pages/EvalBatches": "改用 pages/eval/*",
    "EvalBatches": "改用 pages/eval/*",
}

IMPORT_RE = re.compile(r"""(?:from\s+|import\s*\(\s*)['"]([^'"]+)['"]""")


def _strip_relative(specifier: str) -> str:
    """把 ``../api/stream`` 归一化为 ``api/stream``，便于按尾部匹配。"""
    value = specifier
    while True:
        if value.startswith("./"):
            value = value[2:]
        elif value.startswith("../"):
            value = value[3:]
        elif value.startswith("/"):
            value = value[1:]
        else:
            return value


def _scan_backend(path: Path) -> Iterator[str]:
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("#", "//")):
            continue
        for pattern, hint in BACKEND_PATTERNS:
            if pattern.search(line):
                yield f"{path}:{lineno}: {hint}"


def _scan_frontend(path: Path) -> Iterator[str]:
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        for match in IMPORT_RE.finditer(line):
            specifier = _strip_relative(match.group(1))
            hint = FORBIDDEN_SPECIFIERS.get(specifier)
            if hint:
                yield f"{path}:{lineno}: 旧路径 {match.group(1)}，{hint}"


def main() -> int:
    violations: list[str] = []

    for base in (ROOT / "src", ROOT / "tests"):
        for path in sorted(base.rglob("*.py")):
            violations.extend(_scan_backend(path))

    for path in sorted((ROOT / "web" / "src").rglob("*")):
        if path.suffix in {".ts", ".tsx"}:
            violations.extend(_scan_frontend(path))

    if violations:
        print("发现已删除兼容门面的引用：", file=sys.stderr)
        for line in violations:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("legacy import guard: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
