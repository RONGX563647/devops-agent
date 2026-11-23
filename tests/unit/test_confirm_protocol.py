"""确认协议测试：风险分级、规则作用域、事件投影保真度。"""

from __future__ import annotations

from typing import Any

import pytest
from agentscope.event import RequireUserConfirmEvent
from agentscope.message import ToolCallBlock

from devops_agent.runtime.confirm import (
    ConfirmDecision,
    ExternalAnswer,
    classify,
    command_of,
    effective_scope,
    parse_decisions,
    parse_external_answers,
    rule_content_for,
    validate_answers,
)
from devops_agent.runtime.events import event_to_dict


def _bash(command: str) -> str:
    return f'{{"command": "{command}"}}'


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("Bash", _bash("rm -rf dist"), "danger"),
        ("Bash", _bash("mkfs.ext4 /dev/sda1"), "danger"),
        ("Bash", _bash("ls -la"), "exec"),
        ("Bash", _bash("sed -i 's/a/b/' app.py"), "write"),
        ("Bash", _bash("touch note.md"), "write"),
        ("Write", '{"file_path": "a.txt", "content": "x"}', "write"),
        ("Read", '{"file_path": "a.txt"}', "read"),
    ],
)
def test_classify_marks_risk(name: str, raw: str, expected: str) -> None:
    assert classify(name, raw) == expected


def test_command_of_prefers_command_then_path() -> None:
    assert command_of("Bash", _bash("pytest -q")) == "pytest -q"
    assert command_of("Write", '{"file_path": "x/y.py"}') == "x/y.py"
    assert command_of("Bash", "not json") == ""


def test_rule_content_scopes() -> None:
    raw = _bash("pytest -q tests")
    assert rule_content_for("once", "Bash", raw) is None
    assert rule_content_for("exact", "Bash", raw) == "pytest -q tests"
    assert rule_content_for("prefix", "Bash", raw) == "pytest:*"
    # 没有命令可依据时不给规则，避免生成一条匹配一切的规则
    assert rule_content_for("prefix", "Bash", "{}") is None


def test_effective_scope_downgrades_danger_to_once() -> None:
    """一秒钟的勾选不该变成永久放行 rm -rf。"""
    assert effective_scope("Bash", _bash("rm -rf /"), "prefix") == "once"
    assert effective_scope("Bash", _bash("pytest"), "prefix") == "prefix"


def test_parse_decisions_normalizes_scope_and_skips_blank() -> None:
    decisions = parse_decisions(
        [
            {"tool_call_id": "a", "confirmed": True, "scope": "session"},  # 非法 → once
            {"tool_call_id": "", "confirmed": True},
            {"tool_call_id": "b", "confirmed": False},
        ]
    )
    assert decisions == [
        ConfirmDecision(tool_call_id="a", confirmed=True, scope="once"),
        ConfirmDecision(tool_call_id="b", confirmed=False, scope="once"),
    ]


def test_event_projection_keeps_tool_calls_for_confirm() -> None:
    """回归点：投影层此前把 tool_calls 压成一行工具名，前端拿不到命令原文。"""
    block = ToolCallBlock(id="c1", name="Bash", input=_bash("rm -rf dist"), created_at="")
    data = event_to_dict(RequireUserConfirmEvent(reply_id="r1", tool_calls=[block]))

    assert data["type"] == "REQUIRE_USER_CONFIRM"
    assert data["tool_call_name"] == "Bash"
    calls = data["tool_calls"]
    assert isinstance(calls, list) and len(calls) == 1
    assert calls[0]["tool_call_id"] == "c1"
    assert calls[0]["input"] == "rm -rf dist"
    assert calls[0]["risk"] == "danger"


class _DuckEvent:
    """无 model_dump 的假事件：验证 fallback 路径也不丢 tool_calls。"""

    def __init__(self) -> None:
        self.type = "REQUIRE_USER_CONFIRM"
        self.reply_id = "r2"
        self.tool_calls: list[Any] = [
            {"id": "c9", "name": "Bash", "input": _bash("ls"), "state": "asking"}
        ]


def test_duck_typed_event_keeps_tool_calls() -> None:
    data = event_to_dict(_DuckEvent())
    assert data["tool_calls"][0]["tool_call_id"] == "c9"
    assert data["tool_calls"][0]["risk"] == "exec"


# --- AskUser 外部执行 -------------------------------------------------

_QUESTIONS = [
    {
        "question": "用哪个库？",
        "header": "Library",
        "options": [
            {"label": "httpx", "description": "async 优先"},
            {"label": "requests", "description": "同步惯用"},
        ],
        "multi_select": False,
    },
    {
        "question": "要开哪些能力？",
        "header": "Features",
        "options": [
            {"label": "缓存", "description": "命中率"},
            {"label": "重试", "description": "失败重放"},
        ],
        "multi_select": True,
    },
]


def test_parse_external_answers_skips_blank_id_and_non_dict() -> None:
    parsed = parse_external_answers(
        [
            {"tool_call_id": "a", "answers": [{"question": "q", "selected": []}, "junk"]},
            {"tool_call_id": "", "answers": []},
            {"tool_call_id": "b", "answers": "not-a-list"},
        ]
    )
    assert parsed == [
        ExternalAnswer(tool_call_id="a", answers=({"question": "q", "selected": []},)),
        ExternalAnswer(tool_call_id="b", answers=()),
    ]


def test_validate_answers_keeps_server_authoritative_text() -> None:
    normalized = validate_answers(
        _QUESTIONS,
        (
            {"question": "用哪个库？", "selected": ["httpx"]},
            {"question": "要开哪些能力？", "selected": ["缓存", "重试"], "other": "  "},
        ),
    )
    assert normalized == [
        {"question": "用哪个库？", "selected": ["httpx"], "other": None},
        {"question": "要开哪些能力？", "selected": ["缓存", "重试"], "other": None},
    ]


def test_validate_answers_accepts_other_only() -> None:
    normalized = validate_answers(
        _QUESTIONS[:1],
        ({"question": "用哪个库？", "selected": [], "other": " aiohttp "},),
    )
    assert normalized == [{"question": "用哪个库？", "selected": [], "other": "aiohttp"}]


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        (
            (
                {"question": "用哪个库？", "selected": ["httpx"]},
                {"question": "用哪个库？", "selected": ["requests"]},
            ),
            "问题重复回答",
        ),
        ((), "缺少问题的答案"),
        (({"question": "用哪个库？", "selected": ["urllib"]},), "不存在的选项"),
        (({"question": "用哪个库？", "selected": ["httpx", "requests"]},), "单选题只能选一个选项"),
        (({"question": "用哪个库？", "selected": [], "other": 1},), "other 必须是字符串"),
        (({"question": "用哪个库？", "selected": []},), "问题未作答"),
    ],
)
def test_validate_answers_rejects_bad_payload(
    answers: tuple[dict[str, Any], ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_answers(_QUESTIONS[:1], answers)


def test_validate_answers_rejects_blank_question() -> None:
    with pytest.raises(ValueError, match="答案缺少 question"):
        validate_answers(_QUESTIONS[:1], ({"question": "  ", "selected": []},))
