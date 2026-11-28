"""轮次边界与注入消息的唯一判定点（SPEC-0015 §5）。

「哪条消息是一次真实的用户输入」这条规则原先抄在两处（短期裁剪与召回取
query），且用 `msg.name` 当锚点：注入侧必须记得写 `name="system"`，任何模块
改个命名就会让裁剪和召回朝不同方向坏，而且只有一处能被测试发现。

现在锚点是 `metadata["injected"]`，`name` 只作为旧快照的兼容兜底。所有
user 角色的注入消息都必须经 `injected_note()` 构造。
"""

from __future__ import annotations

from typing import Any

from agentscope.message import Msg, UserMsg

INJECTED_METADATA_KEY = "injected"

# 旧快照里的注入消息没有 metadata，只能靠名字识别；下个大版本删除。
_LEGACY_INJECTED_NAMES = frozenset({"system", "user"})


def injected_note(text: str, *, kind: str) -> Msg:
    """构造一条注入用的 user 消息：打标记，不改历史，只用于本次请求。"""
    return UserMsg(name="system", content=text, metadata={INJECTED_METADATA_KEY: kind})


def is_injected(msg: Any) -> bool:
    """这条 user 消息是不是注入的（而非用户真的说了什么）。"""
    metadata = getattr(msg, "metadata", None)
    if isinstance(metadata, dict) and metadata.get(INJECTED_METADATA_KEY):
        return True
    return getattr(msg, "name", "") in _LEGACY_INJECTED_NAMES


def is_user_turn(msg: Any) -> bool:
    """一次真实用户输入 = user 角色且不是注入。

    轮次裁剪与召回 query 都只认这个判定，不要各自再写一份。
    """
    return getattr(msg, "role", "") == "user" and not is_injected(msg)


__all__ = ["INJECTED_METADATA_KEY", "injected_note", "is_injected", "is_user_turn"]
