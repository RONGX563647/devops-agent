"""全库误伤门禁：源码树过脱敏必须逐字节不变。

这条用例把一次真实审计的结论钉住——111 个文件曾被打上 `{{secret:…}}`，
`def estimate_prefix_tokens` 变成 `def {{secret:out_entropy_771a9e4b}}`，
模型读到的源码不可编译、不可 grep。修法见 SPEC-0015 §3，这里是它的验收线。

例外清单默认空：真要在源码里写一个「像凭据」的样例，必须显式登记并说明理由。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from devops_agent.input.pipeline.redact import redact_preview
from devops_agent.redaction import rules
from devops_agent.secrets.capture import capture_output

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ALLOWLIST = Path(__file__).resolve().parents[1] / "fixtures" / "redaction_allowlist.json"


def _allowlisted() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    data: Any = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    return set(data if isinstance(data, list) else [])


def test_source_tree_is_untouched_by_redaction() -> None:
    """输入侧与代码档案都不许改写源码；命中即为误报。"""
    allowed = _allowlisted()
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if redact_preview(text) != text:
            offenders.append(f"input:{rel}")
        captured, _ = capture_output(
            text, register=lambda item: item.name, profile=rules.PROFILE_CODE
        )
        if captured != text:
            offenders.append(f"code:{rel}")
    assert offenders == [], "脱敏误伤源码：" + "、".join(offenders)


def test_allowlist_is_empty_by_default() -> None:
    """例外清单必须保持为空；新增条目要在 PR 里说明理由。"""
    assert _allowlisted() == set()
