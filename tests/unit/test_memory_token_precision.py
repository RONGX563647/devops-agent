"""Token 分族精度与计数口径（SPEC-0016 §10 A24）。

这一组钉的是「估算到底准不准」，而不是「系数等于多少」：
fixture 的 `shapes[]` 是 tiktoken 实测的五类文本（中文/英文/中英混排/代码/JSON/
路径），每类 9 段。断言的是**偏差包络**：

- 分形状总额 ≤5%（系数拟合得对不对）；
- 单段 ≤25%（英文散文是已知高估方向，见 §4.6：单一 ASCII 系数必然在
  「常见词散文」与「紧凑 JSON」之间取中，宁高不低）；
- 固定前缀（系统提示 + 工具 schema）≤2%，锚点是线上 `usage.prompt_tokens`。
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

from agentscope.message import Msg, ToolResultBlock, UserMsg

from devops_agent.memory import calibration
from devops_agent.memory.runtime import estimate_prefix_tokens
from devops_agent.memory.tokens import FAMILIES, Tokenizer, build, count_messages
from devops_agent.runtime.prompt import build_system_prompt
from devops_agent.tools.schema import tool_schemas

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "token_calibration.json"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _estimate(row: dict[str, Any], family: str) -> int:
    """按行内三族字符数复算估算值（fixture 不存原文，只存字符分类）。"""
    cjk_pt, letter_pt, symbol_pt = FAMILIES[family]
    return math.ceil(
        row["cjk"] / cjk_pt + row["letters"] / letter_pt + row["symbols"] / symbol_pt
    )


def test_fixture_covers_every_shape() -> None:
    """六个形状都在，且每族都有实测真值：缺形状等于这道门失效。"""
    data = _fixture()
    shapes = {row["shape"] for row in data["shapes"]}
    assert shapes == {"zh", "en", "mix", "code", "json", "path", "log"}
    for row in data["shapes"]:
        assert {"cl100k", "o200k"} <= set(row["tokens"])


def test_shape_total_and_segment_error_stay_within_band() -> None:
    """A24：分形状总额 ≤5%，单段 ≤25%。"""
    data = _fixture()
    total_tolerance = float(data["shape_total_tolerance"])
    segment_tolerance = float(data["shape_segment_tolerance"])
    for family in ("cl100k", "o200k"):
        rows = data["shapes"]
        estimated = sum(_estimate(row, family) for row in rows)
        actual = sum(int(row["tokens"][family]) for row in rows)
        assert abs(estimated - actual) / actual <= total_tolerance, family
        worst = max(
            abs(_estimate(row, family) - int(row["tokens"][family])) / int(row["tokens"][family])
            for row in rows
        )
        assert worst <= segment_tolerance, f"{family} 最差单段 {worst:.1%}"


def test_prefix_matches_online_truth() -> None:
    """固定前缀以线上 `usage.prompt_tokens` 为锚：偏差 ≤2%。"""
    data = _fixture()
    tools = asyncio.run(tool_schemas())
    current = estimate_prefix_tokens(build_system_prompt(), tools)
    expected = int(data["expected_tokens"])
    assert abs(current - expected) / expected <= float(data["prefix_tolerance"])


def test_tool_result_output_is_counted() -> None:
    """工具结果块的字段是 `output`：旧实现按 `text` 取，等于把它算成 0。"""
    body = "x" * 557
    message = ToolResultBlock(id="1", name="Read", output=body)
    msg = Msg(name="tool", role="assistant", content=[message])
    without = count_messages([], apply_drift=False)
    counted = count_messages([msg], apply_drift=False)
    # 557 字母 ≈ 100 token（5.57 字母/token）+ 1 条消息开销
    assert counted - without >= 95


def test_message_overhead_is_applied_per_message() -> None:
    tokenizer = build(family="qwen", per_message_overhead=4)
    messages = [
        UserMsg(name="u1", content="hi"),
        UserMsg(name="u2", content="hi"),
    ]
    assert tokenizer.count_messages(messages) == tokenizer.count("hi") * 2 + 8


def test_encoder_bypasses_coefficients() -> None:
    """装了就用的 L1：编码器存在时不应再走系数估算。"""

    class _Encoder:
        def encode(self, text: str) -> list[str]:
            return text.split()

    tokenizer = Tokenizer(encoder=_Encoder())
    assert tokenizer.precise is True
    assert tokenizer.count("a b c") == 3


def test_drift_is_ignored_until_enough_samples(monkeypatch: Any, tmp_path: Any) -> None:
    """单条样本的比值噪声太大：不足 3 条不修正，够 3 条才动 α。"""
    monkeypatch.setattr(calibration, "_path", lambda: tmp_path / "calibration.json")
    calibration.reset()
    try:
        for _ in range(2):
            assert calibration.observe(100, 130)[0] == "ok"
        state, alpha = calibration.observe(100, 130)
        assert state == "error"
        assert alpha == 1.3
        assert calibration.drift()["samples"] == 3
    finally:
        calibration.reset()


def test_small_drift_is_reported_as_adjusted(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr(calibration, "_path", lambda: tmp_path / "calibration.json")
    calibration.reset()
    try:
        for _ in range(3):
            state, alpha = calibration.observe(1000, 1050)
        assert state == "adjusted"
        assert 1.04 <= alpha <= 1.06
    finally:
        calibration.reset()


def test_zero_samples_are_skipped(monkeypatch: Any, tmp_path: Any) -> None:
    """拿不到实测或估算为 0 时不该记账：会把中位数往 0 拉。"""
    monkeypatch.setattr(calibration, "_path", lambda: tmp_path / "calibration.json")
    calibration.reset()
    try:
        calibration.observe(0, 100)
        calibration.observe(100, 0)
        assert calibration.drift()["samples"] == 0
        assert calibration.alpha() == 1.0
    finally:
        calibration.reset()
