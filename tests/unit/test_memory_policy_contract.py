"""记忆配置的契约测试（SPEC-0013 §18.5 A16 / A17）。

这一组不测行为，只钉住「配置里有这个键，就一定有代码读它」：
死配置不会报错，只会让调参的人以为改了有效果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.memory_policy import EpisodicPolicy, LongTermPolicy, MemoryPolicy

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "memory.yaml"


def _leaves(node: dict[str, Any]) -> set[str]:
    """递归收集内存配置的叶子键名（只关心键，不关心值）。"""
    keys: set[str] = set()
    for name, value in node.items():
        keys.add(name)
        if isinstance(value, dict):
            keys |= _leaves(value)
    return keys


def test_memory_yaml_schema_is_loadable() -> None:
    """yaml 能过 MemoryPolicy：删键后残留的旧键被忽略，不会把服务拦在门外。"""
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert MemoryPolicy.model_validate(raw["memory"]).enabled is True


def test_trigger_turns_is_gone() -> None:
    """A17：`trigger_turns` 没有被任何代码消费，字段与 yaml 键都必须消失。

    这里用结构断言而不是「构造应报错」：pydantic 默认忽略未知键，
    要让它报错就得给 EpisodicPolicy 加 `extra="forbid"`，那会让存量
    config.db 里残留的 `trigger_turns` 覆盖把整份配置打成无效。
    """
    assert "trigger_turns" not in EpisodicPolicy.model_fields
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert "trigger_turns" not in raw["memory"]["episodic"]


def test_write_on_rejects_session_end() -> None:
    """A17：`session_end` 从未实现，写它应该在加载配置时就报错。"""
    with pytest.raises(ValidationError):
        LongTermPolicy(write_on="session_end")  # type: ignore[arg-type]
    assert LongTermPolicy().write_on == "turn"


def test_every_yaml_key_has_a_consumer() -> None:
    """A17：memory.yaml 的每个键在 src 里都有读它的地方（memory_policy 自身除外）。"""
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    keys = _leaves(raw["memory"])
    assert keys
    body = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src").rglob("*.py")
        if path.name != "memory_policy.py"
    )
    missing = sorted(key for key in keys if key not in body)
    assert missing == [], f"以下配置键没有任何消费点：{missing}"


def test_status_separates_requested_and_effective() -> None:
    """A16：配 mem0 时 requested=mem0、effective=local、reason 非空。"""
    policy = MemoryPolicy(long_term=LongTermPolicy(backend="mem0"))
    status = MemoryRuntime("s1", policy, MemorySink(), None).status()
    assert status["requested"] == "mem0"
    assert status["effective"] == "local"
    # backend 是 effective 的别名，老消费方（/health、前端）不受影响
    assert status["backend"] == "local"
    assert "未接入" in status["reason"]


def test_auto_resolves_to_local_without_noise() -> None:
    """auto 不是降级：解析到 local 且不该产生 reason，否则启动日志会被刷屏。"""
    status = MemoryRuntime("s1", MemoryPolicy(), MemorySink(), None).status()
    assert status["requested"] == "auto"
    assert status["effective"] == "local"
    assert status["reason"] == ""
