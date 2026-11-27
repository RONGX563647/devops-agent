"""上下文预算与记忆策略测试（SPEC-0013 §5.4）。"""

from devops_agent.config import clear_cache, load_memory
from devops_agent.memory.budget import compute_budget
from devops_agent.memory.long_term import resolve_scope_id
from devops_agent.memory.tokens import clip_to_tokens, estimate_tokens
from devops_agent.memory_policy import LongTermPolicy, MemoryPolicy


def _budget(**overrides: object):
    kwargs = dict(
        context_size=128000,
        trigger_ratio=0.8,
        reserve_ratio=0.1,
        max_tokens=None,
        prefix_tokens=8000,
        short_term_ratio=0.5,
        episodic_ratio=0.3,
        long_term_ratio=0.1,
        headroom_ratio=0.1,
        episodic_enabled=True,
        long_term_enabled=True,
    )
    kwargs.update(overrides)
    return compute_budget(**kwargs)  # type: ignore[arg-type]


def test_budget_subtracts_prefix_and_output_reserve() -> None:
    """Hmax = 0.8×128000 − 8000 − 12800 = 81600。"""
    budget = _budget()
    assert budget.history_max == 81600
    assert budget.short_term == 40800
    assert budget.episodic == 24480
    assert budget.long_term == 8160
    assert budget.headroom == 8160


def test_budget_uses_max_tokens_as_output_reserve() -> None:
    budget = _budget(max_tokens=4000)
    assert budget.output_reserve == 4000
    assert budget.history_max == 102400 - 8000 - 4000


def test_budget_never_negative_when_prefix_too_large() -> None:
    budget = _budget(prefix_tokens=200000)
    assert budget.history_max == 0
    assert budget.short_term == 0


def test_disabled_layers_move_share_to_short_term() -> None:
    budget = _budget(episodic_enabled=False, long_term_enabled=False)
    assert budget.episodic == 0
    assert budget.long_term == 0
    assert budget.short_term == int(81600 * 0.9)


def test_clip_to_tokens_keeps_text_within_budget() -> None:
    text = "中文内容" * 400
    clipped = clip_to_tokens(text, 50)
    assert 0 < len(clipped) < len(text)
    assert estimate_tokens(clipped) <= 50
    assert clip_to_tokens(text, 0) == ""
    assert clip_to_tokens("短", 100) == "短"


def test_resolve_scope_id_switches_on_scope_and_user() -> None:
    session = MemoryPolicy(user_id="u1", long_term=LongTermPolicy(scope="session"))
    user = MemoryPolicy(user_id="u1", long_term=LongTermPolicy(scope="user"))
    user_without_id = MemoryPolicy(user_id="", long_term=LongTermPolicy(scope="user"))
    assert resolve_scope_id(session, "s1") == "session:s1"
    assert resolve_scope_id(user, "s1") == "u1"
    assert resolve_scope_id(user_without_id, "s1") == "session:s1"


def test_load_memory_reads_yaml_and_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    clear_cache()
    policy = load_memory()
    assert policy.short_term.max_turns == 12
    assert policy.long_term.backend == "auto"

    monkeypatch.setenv("DEVOPS_AGENT_MEMORY_ENABLED", "0")
    monkeypatch.setenv("DEVOPS_AGENT_MEMORY_BACKEND", "off")
    clear_cache()
    overridden = load_memory()
    assert overridden.enabled is False
    assert overridden.long_term.backend == "off"
    clear_cache()
