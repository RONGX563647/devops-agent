"""输出侧敏感信息捕获：识别明文 → 登记入库 → 换成占位符。

重点钉住两条边界：
- 工具自己读到的明文（不在注入集合里）必须被捕获，模型上下文不许出现原文；
- 普通文本/日志不能误伤（熵不够、无凭据语义词就放行）。
"""

from __future__ import annotations

import math

from devops_agent.secrets.capture import (
    capture_output,
    find_secrets,
    shannon_entropy,
)

KEY = "AKIA" + "A" * 16
OPENAI = "sk-" + "aB3xY9" * 4


def test_finds_known_prefixes() -> None:
    text = f"aws={KEY} openai={OPENAI}"
    found = find_secrets(text)
    values = {item.value for item in found}
    assert KEY in values
    assert OPENAI in values


def test_finds_assigned_high_entropy_value() -> None:
    value = "Tr0ub4dor3xKq9mZpL2nQ"  # 高熵、长度 ≥20
    found = find_secrets(f'db_password = "{value}"')
    assert any(item.value == value for item in found)


def test_plain_log_not_captured() -> None:
    """普通日志/低熵串不该被误伤。"""
    assert find_secrets("BUILD SUCCESS in 3.2s, 42 files changed") == []


def test_short_token_near_keyword_not_captured() -> None:
    """有语义词但太短的串（如 `token=abc`）放行，避免抹掉正常输出。"""
    assert find_secrets("token=abc123") == []


def test_entropy_distinguishes_random_from_repetitive() -> None:
    assert shannon_entropy("aaaaaaaaaaaaaaaaaaaa") < 3.5
    assert shannon_entropy("aB3xY9qN8vL2mK7pR4wZ") > 3.5
    assert math.isclose(shannon_entropy("a"), 0.0)


def test_capture_replaces_with_placeholder_and_reports() -> None:
    registered: list[str] = []

    def register(item: object) -> str:  # type: ignore[no-untyped-def]
        registered.append(item.name)  # type: ignore[attr-defined]
        return item.name  # type: ignore[attr-defined]

    out, captured = capture_output(f"key found: {KEY}", register=register)
    assert KEY not in out
    assert "{{secret:" in out
    assert len(captured) == 1
    assert captured[0].value == KEY
    assert registered == [captured[0].name]


def test_capture_idempotent_for_no_secrets() -> None:
    def register(item: object) -> str:  # type: ignore[no-untyped-def]
        raise AssertionError("不该被调用")

    out, captured = capture_output("just a normal line", register=register)
    assert out == "just a normal line"
    assert captured == []
