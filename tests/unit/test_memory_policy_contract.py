"""记忆配置的契约测试（SPEC-0013 §18.5 A16 / A17）。

这一组不测行为，只钉住两条：配置项必须有人消费（死配置不会报错，只会让
调参的人以为改了有效果）；降级必须如实上报。

基线在代码里（`MemoryPolicy` 的字段默认），`config/*.yaml` 已不再抄一份；
可写覆盖只走 config.db，所以这里直接对着模型字段做结构断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from devops_agent import config as cfg
from devops_agent import config_store as cs
from devops_agent.memory.runtime import MemoryRuntime
from devops_agent.memory.sink import MemorySink
from devops_agent.memory_policy import EpisodicPolicy, LongTermPolicy, MemoryPolicy

ROOT = Path(__file__).resolve().parents[2]


def _leaves(model: type[BaseModel], prefix: str = "") -> list[str]:
    """递归收集叶子字段路径（嵌套模型展开成 `budget.short_term_ratio`）。"""
    out: list[str] = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        path = f"{prefix}{name}"
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.extend(_leaves(annotation, f"{path}."))
        else:
            out.append(path)
    return out


def test_memory_defaults_load_without_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 yaml 基线时全回落代码默认：配置目录为空不该把服务拦在门外。"""
    monkeypatch.setenv(cfg.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(cs.MODE_ENV, "file")
    cfg.clear_cache()
    cs.invalidate()
    try:
        assert cfg.load_memory() == MemoryPolicy()
    finally:
        cs.invalidate()


def test_trigger_turns_is_gone() -> None:
    """A17：`trigger_turns` 没有被任何代码消费，字段必须消失。

    这里用结构断言而不是「构造应报错」：pydantic 默认忽略未知键，
    要让它报错就得给 EpisodicPolicy 加 `extra="forbid"`，那会让存量
    config.db 里残留的 `trigger_turns` 覆盖把整份配置打成无效。
    """
    assert "trigger_turns" not in EpisodicPolicy.model_fields


def test_write_on_rejects_session_end() -> None:
    """A17：`session_end` 从未实现，写它应该在加载配置时就报错。"""
    with pytest.raises(ValidationError):
        LongTermPolicy(write_on="session_end")  # type: ignore[arg-type]
    assert LongTermPolicy().write_on == "turn"


def test_every_policy_field_has_a_consumer() -> None:
    """A17：MemoryPolicy 的每个叶子字段在 src 里都有读它的地方。"""
    keys = _leaves(MemoryPolicy)
    assert keys
    body = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src").rglob("*.py")
        if path.name != "memory_policy.py"
    )
    missing = sorted(key for key in keys if key.split(".")[-1] not in body)
    assert missing == [], f"以下配置字段没有任何消费点：{missing}"


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
