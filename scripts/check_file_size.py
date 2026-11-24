"""文件行数棘轮：存量超标文件只能变短，新文件不得超过上限。

阈值唯一来源是 config/gates.yaml；基线记录在 config/size-baseline.json。
拆分长文件后应同步下调基线，未下调只提示不失败，避免无关改动被卡住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "config" / "size-baseline.json"
GATES = ROOT / "config" / "gates.yaml"
PATTERNS = ("src/**/*.py", "web/src/**/*.ts", "web/src/**/*.tsx")


def _limit() -> int:
    # 只读取一个整数，避免为一行配置引入 PyYAML 之外的依赖。
    for line in GATES.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("max_lines:"):
            raw = stripped.split(":", 1)[1].split("#", 1)[0].strip()
            return int(raw)
    return 500


def _is_test(path: Path) -> bool:
    return path.name.endswith((".test.ts", ".test.tsx")) or "tests" in path.parts


def _count(path: Path) -> int:
    with path.open(encoding="utf-8", errors="ignore") as handle:
        return sum(1 for _ in handle)


def main() -> int:
    limit = _limit()
    baseline: dict[str, int] = json.loads(BASELINE.read_text(encoding="utf-8"))
    violations: list[str] = []
    notices: list[str] = []
    seen: set[str] = set()

    for pattern in PATTERNS:
        for path in ROOT.glob(pattern):
            if _is_test(path):
                continue
            rel = path.relative_to(ROOT).as_posix()
            seen.add(rel)
            lines = _count(path)
            allowed = baseline.get(rel, limit)
            if lines > allowed:
                violations.append(
                    f"{rel}: {lines} 行，超过允许的 {allowed} 行"
                    + ("（存量基线，只能下降）" if rel in baseline else "（新文件上限）")
                )
            elif rel in baseline and lines < baseline[rel]:
                notices.append(f"{rel}: {lines} 行，基线 {baseline[rel]}，请同步下调")

    for rel in baseline:
        if rel not in seen:
            notices.append(f"{rel}: 基线条目已失效，请删除")

    for notice in notices:
        print(f"note: {notice}")
    if violations:
        print("\n".join(f"error: {item}" for item in violations), file=sys.stderr)
        print(
            f"\n共 {len(violations)} 个文件超限。请拆分职责，或下调 config/size-baseline.json。",
            file=sys.stderr,
        )
        return 1
    print(f"file size ratchet: ok (limit={limit}, baseline={len(baseline)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
