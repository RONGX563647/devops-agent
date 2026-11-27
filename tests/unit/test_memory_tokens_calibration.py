"""Token 口径可校准（SPEC-0015 §6）。

旧状况：系数只在 docstring 里有一个校准样本，测试带宽 3000–4500（±25%），
任何 20% 的漂移都能通过；而 docstring 里的字符数早就被真实提示词超过了。
这里把样本挪进 fixture，并把「提示词长大了」变成红灯。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentscope.agent import ContextConfig
from agentscope.message import SystemMsg, UserMsg

from devops_agent.config import load_memory
from devops_agent.memory import events
from devops_agent.memory.budget import compute_budget
from devops_agent.memory.observer import MemoryMiddleware
from devops_agent.memory.runtime import MemoryRuntime, estimate_prefix_tokens
from devops_agent.memory.sink import MemorySink
from devops_agent.memory.tokens import ASCII_CHARS_PER_TOKEN, CJK_CHARS_PER_TOKEN, estimate_tokens
from devops_agent.runtime.prompt import build_system_prompt
from devops_agent.tools.schema import tool_schemas

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "token_calibration.json"


def _sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_unit_coefficients_are_pinned() -> None:
    """两个系数是契约：改它们必须同时改 fixture，否则这里的比值立刻不对。"""
    assert CJK_CHARS_PER_TOKEN == 1.4
    assert ASCII_CHARS_PER_TOKEN == 3.6
    assert estimate_tokens("汉" * 140) == 100
    assert estimate_tokens("a" * 360) == 100
    assert estimate_tokens("汉" * 7 + "a" * 18) == 10


def test_current_prefix_stays_within_calibration_tolerance() -> None:
    """提示词或工具集变大超过阈值即失败：请重新跑一次实测并更新 fixture。"""
    sample = _sample()
    tools = asyncio.run(tool_schemas())
    prompt = build_system_prompt()
    current = estimate_prefix_tokens(prompt, tools)
    expected = int(sample["expected_tokens"])
    drift = abs(current - expected) / expected
    assert drift <= float(sample["drift_tolerance"]), (
        f"前缀估算 {current} 与校准样本 {expected} 偏差 {drift:.1%}，"
        "说明提示词/工具集已变更，请重新校准 tests/fixtures/token_calibration.json"
    )


def test_estimate_matches_sample_within_band() -> None:
    """同一份样本上估算误差不超过 8%；字符数偏差用更宽的漂移阈值单独判。"""
    sample = _sample()
    tools = asyncio.run(tool_schemas())
    prompt = build_system_prompt()
    current = estimate_prefix_tokens(prompt, tools)
    expected = int(sample["expected_tokens"])
    assert abs(current - expected) / expected <= float(sample["tolerance"])
    chars = int(sample["system_chars"])
    assert abs(len(prompt) - chars) / chars <= float(sample["drift_tolerance"])


def _budget(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = dict(
        context_size=128000,
        trigger_ratio=0.8,
        reserve_ratio=0.1,
        max_tokens=None,
        prefix_tokens=8000,
        short_term_ratio=0.5,
        episodic_ratio=0.3,
        long_term_ratio=0.1,
        headroom_ratio=0.1,
    )
    kwargs.update(overrides)
    return compute_budget(**kwargs)


def test_margin_ratio_only_shrinks_history() -> None:
    """安全垫只作用在固定前缀上，按比例减小历史面额度。"""
    plain = _budget()
    padded = _budget(estimate_margin_ratio=0.1)
    assert padded.fixed == 8800
    assert padded.history_max == plain.history_max - 800
    assert padded.short_term < plain.short_term


def test_zero_margin_keeps_legacy_numbers() -> None:
    """默认 0 保持纯函数口径，存量调用与快照不受影响。"""
    assert _budget(estimate_margin_ratio=0.0).history_max == 81600


class _FakeModel:
    context_size = 128000

    async def count_tokens(self, **_kwargs: Any) -> int:
        return 100


class _FakeState:
    def __init__(self) -> None:
        self.summary = ""
        self.context: list[Any] = []
        self.middle_context: dict[str, Any] = {}
        self.reply_id = "r1"


class _FakeAgent:
    def __init__(self) -> None:
        self.model = _FakeModel()
        self.state = _FakeState()
        self.context_config = ContextConfig()


def _runtime(tmp_path: Any) -> MemoryRuntime:
    policy = load_memory().model_copy(
        update={"context": load_memory().context.model_copy(update={"estimate_audit": True})}
    )
    return MemoryRuntime("s1", policy, MemorySink(), tmp_path / "obs.db")


def test_estimate_delta_is_emitted_once_per_reply(tmp_path: Any) -> None:
    runtime = _runtime(tmp_path)
    middleware = MemoryMiddleware(runtime)
    agent = _FakeAgent()
    usage = SimpleNamespace(input_tokens=900, cache_input_tokens=100, cache_creation_input_tokens=0)

    async def handler(**_kwargs: Any) -> Any:
        return SimpleNamespace(usage=usage)

    messages = [SystemMsg(name="system", content="sys"), UserMsg(name="s1", content="继续")]
    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, handler))
    asyncio.run(middleware.on_model_call(agent, {"messages": messages, "tools": []}, handler))
    deltas = [
        frame for frame in runtime.sink.drain() if frame["type"] == events.CONTEXT_ESTIMATE_DELTA
    ]
    assert len(deltas) == 1
    assert deltas[0]["memory"]["actual"] == 1000
    assert deltas[0]["memory"]["estimated"] > 0
    assert deltas[0]["memory"]["ratio"] > 0
