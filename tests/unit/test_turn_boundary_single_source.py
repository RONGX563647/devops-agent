"""轮次边界只有一处实现（SPEC-0015 §5）。

旧状况：短期裁剪与召回各写一份 `role == "user" and name not in ("system", "user")`，
锚点是名字；注入侧改个命名，裁剪与召回就会朝不同方向坏，而且只有一处能被测到。
"""

from __future__ import annotations

from pathlib import Path

from agentscope.message import Msg, TextBlock, UserMsg

from devops_agent.memory.short_term import trim_messages
from devops_agent.memory.turns import (
    INJECTED_METADATA_KEY,
    injected_note,
    is_injected,
    is_user_turn,
)
from devops_agent.memory_policy import ShortTermPolicy

ROOT = Path(__file__).resolve().parents[2]


def _text(value: str) -> list[TextBlock]:
    return [TextBlock(type="text", text=value, id="b", created_at="")]


def test_no_module_but_turns_constructs_injected_user_messages() -> None:
    """注入消息必须走 `injected_note()`，否则判定会再次分叉。"""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if path.name != "turns.py" and 'UserMsg(name="system"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_predicate_is_not_duplicated() -> None:
    """旧谓词的字符串不许再出现在任何模块里。"""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if 'name not in ("system", "user")' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_injected_note_is_not_a_user_turn() -> None:
    note = injected_note("## 会话记忆\n…", kind="memory")
    assert note.metadata[INJECTED_METADATA_KEY] == "memory"
    assert is_injected(note) is True
    assert is_user_turn(note) is False


def test_real_input_is_a_user_turn() -> None:
    assert is_user_turn(UserMsg(name="s1", content="继续")) is True


def test_legacy_snapshot_without_metadata_still_recognised() -> None:
    """旧快照里的注入消息没有 metadata，只能靠名字兜底，否则会被算成新轮次。"""
    legacy = Msg(name="system", role="user", content=_text("旧的记忆注脚"))
    assert is_injected(legacy) is True
    assert is_user_turn(legacy) is False


def _of(msg: Msg) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(getattr(block, "text", "") for block in content)


def test_turn_trimming_counts_only_real_inputs() -> None:
    policy = ShortTermPolicy(max_turns=1)
    messages: list[Msg] = [
        UserMsg(name="s1", content="第一轮"),
        injected_note("记忆注脚", kind="memory"),
        UserMsg(name="s1", content="第二轮"),
        injected_note("档位说明", kind="profile"),
    ]
    trimmed, report = trim_messages(messages, policy)
    assert report.dropped_turns == 1
    assert [_of(msg) for msg in trimmed] == ["第二轮", "档位说明"]
