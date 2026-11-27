"""脱敏规则：形状 / 位置 / 上下文 / 档案四道闸，与跨度替换。

这里的用例全部来自一次真实审计：读源码的工具输出被当成凭据改写成
`def {{secret:out_entropy_771a9e4b}}(...)`，以及 `token:` 换行后的
`continue` 被吞掉。每条断言都对着一类真实误伤。
"""

from __future__ import annotations

from devops_agent.redaction import rules
from devops_agent.secrets.capture import capture_output, find_secrets

RANDOM = "Tr0ub4dor3xKq9mZpL2nQ"
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"


def _name(base: str, value: str) -> str:
    return f"t_{base}"


def _redact_code(text: str) -> str:
    out, _ = rules.redact(text, profile=rules.PROFILE_CODE, name_for=_name)
    return out


def test_identifier_without_digits_is_not_a_secret() -> None:
    """函数名与模块路径是本项目最大的一类误报（415/425）。"""
    text = "def estimate_prefix_tokens(system_text: str) -> int:\n    pass"
    assert _redact_code(text) == text
    assert find_secrets(text, profile=rules.PROFILE_CODE) == []


def test_dotted_path_is_not_a_secret() -> None:
    assert _redact_code("from devops_agent.memory.tokens import estimate_tokens") == (
        "from devops_agent.memory.tokens import estimate_tokens"
    )


def test_annotation_separator_does_not_cross_lines() -> None:
    """`\\s*` 跨行会把下一条语句当成值：这里 `continue` 必须留着。"""
    text = 'if "://" in token:\n            continue'
    out = _redact_code(text)
    assert "continue" in out
    assert "{{secret:" not in out


def test_callee_is_not_a_secret_value() -> None:
    text = '    api_key = os.environ.get("DEVOPS_AGENT_API_KEY")'
    assert _redact_code(text) == text


def test_random_value_with_digits_still_captured() -> None:
    text = f'password: "{RANDOM}"'
    out = _redact_code(text)
    assert RANDOM not in out
    assert "{{secret:t_password}}" in out


def test_mixed_case_without_digits_still_captured() -> None:
    """无数字的 base64 串靠「大小写混合 + 长 + 高熵」兜住。"""
    value = "aBcDeFgHiJkLmNoPqRsTuVwXyZ"
    out = _redact_code(f'token: "{value}"')
    assert value not in out


def test_all_caps_constant_is_not_a_secret() -> None:
    assert _redact_code("API_KEY=DEVOPS_AGENT_API_KEY") == "API_KEY=DEVOPS_AGENT_API_KEY"


def test_known_prefixes_bypass_shape_gate() -> None:
    for value, base in (
        ("AKIA" + "A" * 16, "aws_access_key"),
        ("sk-" + "a1" * 12, "openai_key"),
        (PEM, "private_key"),
    ):
        out, applied = rules.redact(f"x={value}", profile=rules.PROFILE_CODE, name_for=_name)
        assert value not in out
        assert applied[0].base == base


def test_span_replacement_does_not_touch_longer_identifier() -> None:
    """旧实现 `str.replace` 会把这个值在别处的所有出现一起改掉。"""
    text = f'password: "{RANDOM}"\nnote = my_{RANDOM}_suffix'
    out, _ = rules.redact(text, profile=rules.PROFILE_CODE, name_for=_name)
    assert f"my_{RANDOM}_suffix" in out
    assert "{{secret:t_password}}" in out


def test_entropy_requires_value_position() -> None:
    """正文里提到 token 不算证据；只有赋值位置才收。"""
    prose = f"这一轮 token 的统计见日志，候选串 {RANDOM} 只是举例"
    assert find_secrets(prose) == []
    assigned = f"token: {RANDOM}"
    assert [item.value for item in find_secrets(assigned)] == [RANDOM]


def test_existing_placeholder_is_idempotent() -> None:
    text = "Authorization: Bearer {{secret:token}}"
    out, applied = rules.redact(text, profile=rules.PROFILE_INPUT, name_for=_name)
    assert out == text
    assert applied == []


def test_code_profile_skips_entropy_but_output_keeps_it() -> None:
    text = f"config dump: password: {RANDOM}"
    assert find_secrets(text, profile=rules.PROFILE_CODE) != []
    # 仅凭「同行有 password:」不构成熵兜底证据时（无赋值位置）两边都不收
    prose = f"password 字段的值是 {RANDOM}"
    assert find_secrets(prose, profile=rules.PROFILE_OUTPUT) == []


def test_capture_output_reports_confidence() -> None:
    out, captured = capture_output(
        "key=AKIA" + "A" * 16, register=lambda item: item.name, profile=rules.PROFILE_OUTPUT
    )
    assert "AKIA" not in out
    assert captured[0].confidence == rules.CONFIDENCE_KNOWN
