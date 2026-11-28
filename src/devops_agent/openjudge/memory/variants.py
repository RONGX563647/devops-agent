"""变体派生：把 plan 里的配置差量变成可复现的 MemoryPolicy（SPEC §8.1）。

评测基线必须与仓库配置解耦：这里用代码内的确定性默认值构造 policy，
而不是读运行期配置（config.db 覆盖）——否则同一条用例在别人的机器上会换一套阈值。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from devops_agent.memory_policy import (
    EpisodicPolicy,
    LongTermPolicy,
    MemoryBudgetPolicy,
    MemoryContextPolicy,
    MemoryPolicy,
    MemoryTracePolicy,
    ShortTermPolicy,
)

#: 评测用的确定性检索阈值：比生产默认更严，保证「只问其中一条」时干扰项不注入。
EVAL_MIN_SCORE = 0.7


def base_policy(scope_id: str = "") -> MemoryPolicy:
    """评测基线：三层全开、后端固定 local、参数写死，保证跨机器一致。"""
    return MemoryPolicy(
        enabled=True,
        user_id=scope_id,
        short_term=ShortTermPolicy(max_turns=12),
        episodic=EpisodicPolicy(enabled=True),
        long_term=LongTermPolicy(
            enabled=True,
            backend="local",
            scope="user",
            top_k=5,
            min_score=EVAL_MIN_SCORE,
        ),
        budget=MemoryBudgetPolicy(),
        context=MemoryContextPolicy(),
        trace=MemoryTracePolicy(),
    )


def _with_layers(
    policy: MemoryPolicy,
    *,
    episodic: bool = True,
    long_term: bool = True,
) -> MemoryPolicy:
    return policy.model_copy(
        update={
            "episodic": policy.episodic.model_copy(update={"enabled": episodic}),
            "long_term": policy.long_term.model_copy(update={"enabled": long_term}),
        }
    )


def variant_policy(
    name: str, scope_id: str, overrides: dict[str, Any] | None = None
) -> MemoryPolicy:
    """按变体名派生 policy；未知变体按 full 处理，再由 overrides 覆盖。"""
    policy = base_policy(scope_id)
    if name == "off":
        policy = _with_layers(policy, episodic=False, long_term=False).model_copy(
            update={"enabled": False}
        )
    elif name == "short":
        policy = _with_layers(policy, episodic=False, long_term=False)
    elif name == "episodic_only":
        policy = _with_layers(policy, episodic=True, long_term=False)
    elif name == "full_no_compress":
        policy = _with_layers(policy, episodic=False, long_term=True)
    return _apply_overrides(policy, overrides or {})


def _apply_overrides(policy: MemoryPolicy, overrides: dict[str, Any]) -> MemoryPolicy:
    """只认白名单键：写错的键不该悄悄改变结论。"""
    episodic = bool(overrides.get("episodic", policy.episodic.enabled))
    long_term = bool(overrides.get("long_term", policy.long_term.enabled))
    policy = _with_layers(policy, episodic=episodic, long_term=long_term)
    updates: dict[str, Any] = {}
    long_updates: dict[str, Any] = {}
    for key in ("top_k", "min_score", "backend"):
        if key in overrides:
            long_updates[key] = overrides[key]
    if long_updates:
        updates["long_term"] = policy.long_term.model_copy(update=long_updates)
    return policy.model_copy(update=updates)


def config_sha(
    name: str,
    overrides: dict[str, Any],
    *,
    model: str = "deterministic",
    tools: tuple[str, ...] = (),
    permission: str = "readonly",
) -> str:
    """变体配置指纹：变体差量 + 模型 + 工具集 + 权限档位。"""
    body = json.dumps(
        {
            "variant": name,
            "overrides": overrides,
            "model": model,
            "tools": list(tools),
            "permission": permission,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()[:16]
