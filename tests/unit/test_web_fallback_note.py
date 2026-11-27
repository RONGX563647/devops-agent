"""L1 降级信号的文案测试（SPEC-0012 §4.2）。

规则：有缺口才出提示；提示里的下一步取决于**本轮是否授予了 web_search**。
这条边界很容易写反——写反了模型会去调一个不存在的工具，或者明明能搜却被告知用 shell。
"""

from __future__ import annotations

from devops_agent.gateway.turn.orchestrate import _fallback_note


class _Port:
    """最小替身：只实现 `web_search()`，其余能力与文案无关。"""

    def __init__(self, granted: bool | None) -> None:
        self._granted = granted

    def web_search(self) -> bool | None:
        return self._granted


def test_no_gap_means_no_note() -> None:
    assert _fallback_note("", _Port(True)) == ""


def test_note_advises_tool_when_granted() -> None:
    note = _fallback_note("1 条引用内容不足", _Port(True))
    assert note.startswith("\n\n[网络获取] 1 条引用内容不足。")
    assert "web_search" in note
    assert "不要重复抓取" in note


def test_note_advises_shell_when_not_granted() -> None:
    note = _fallback_note("1 条引用未取到", _Port(False))
    assert "web_search" not in note
    assert "shell" in note


def test_note_treats_unknown_capability_as_not_granted() -> None:
    """`None` 表示运行时没有这个概念（Echo / 替身）：不能假设它一定能搜。"""
    note = _fallback_note("1 条引用未取到", _Port(None))
    assert "shell" in note
