"""治理、记忆、评测模块的工厂单元测试。"""

from unittest.mock import MagicMock

from agentscope.permission import PermissionEngine, PermissionMode, PermissionRule

from devops_agent.eval.scorer import EvalDimension
from devops_agent.governance.policy import build_permission_engine, deny_rule
from devops_agent.memory import ledger
from devops_agent.memory.ledger import build_mem0_middleware


def test_deny_rule() -> None:
    rule = deny_rule("write_file")
    assert isinstance(rule, PermissionRule)
    assert rule.tool_name == "write_file"


def test_build_permission_engine() -> None:
    engine = build_permission_engine(
        mode=PermissionMode.EXPLORE,
        deny_rules={"write_file": [deny_rule("write_file")]},
    )
    assert isinstance(engine, PermissionEngine)


def test_build_permission_engine_defaults() -> None:
    assert isinstance(build_permission_engine(), PermissionEngine)


def test_build_mem0_middleware(monkeypatch: object) -> None:
    """monkeypatch 掉 Mem0Middleware，验证工厂传参而不依赖 mem0 异步客户端。"""
    captured: dict[str, object] = {}

    class FakeMem0:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(ledger, "Mem0Middleware", FakeMem0)  # type: ignore[attr-defined]
    fake_client = MagicMock()
    instance = build_mem0_middleware("user-1", client=fake_client, top_k=7)
    assert type(instance).__name__ == "FakeMem0"
    assert captured["user_id"] == "user-1"
    assert captured["top_k"] == 7
    assert captured["client"] is fake_client


def test_eval_dimension_values() -> None:
    assert EvalDimension.CORRECTNESS == "correctness"
    assert EvalDimension.PERFORMANCE == "performance"
    assert EvalDimension.SAFETY == "safety"
    assert set(EvalDimension) == {
        EvalDimension.CORRECTNESS,
        EvalDimension.PERFORMANCE,
        EvalDimension.SAFETY,
    }
