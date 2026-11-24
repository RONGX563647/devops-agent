"""判卷的共享数据结构 —— 轨迹摘要、检查项、判卷结果与硬失败校验。

这些类型被 `graders` 与 `scoring` 同时引用，单独成模块避免循环依赖。
对外仍从 `devops_agent.eval.graders` 导出，调用方无需改 import。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from devops_agent.eval.kinds import HARD_KEYS

_ALLOWED_HARD: frozenset[str] = frozenset(HARD_KEYS)


def _record(hard: list[str], *kinds: str) -> None:
    """把分类记进 hard_failures；未知分类立即报错。"""
    for kind in kinds:
        if kind not in _ALLOWED_HARD:
            raise ValueError(f"未知硬失败分类: {kind}（合法值 {sorted(_ALLOWED_HARD)}）")
        hard.append(kind)


@dataclass(frozen=True, slots=True)
class Check:
    """一条可展示的检查项。"""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TraceResult:
    """runner 收集到的一次运行摘要。

    工具轨迹分三档而不是一档：`tool_names` 只说明「模型发了调用」，
    而一次性运行有三种结局，事件流形态各不相同（实测）：

    - ALLOW：`TOOL_CALL_START` + `TOOL_RESULT_END(state=success|error)`
    - DENY ：`TOOL_CALL_START` + `TOOL_RESULT_END(state=denied)`
    - ASK  ：只有 `TOOL_CALL_START`，事件流止于 `REQUIRE_USER_CONFIRM`，没有 RESULT

    所以「叫过 Bash」不足以说明「跑过 Bash」。旧实现只看 `tool_names`，
    把「被权限拒绝」和「跑成功了」判成同一结果：`c-003` 在 readonly 档下
    `pytest -q` 被 DENY，仍判 pass。
    """

    status: str
    final_text: str
    # 发过调用（含被拒与被挂起）
    tool_names: list[str] = field(default_factory=list)
    # 去重后的 tool_names 只用于命中判断；预算要按真实调用次数算
    tool_call_count: int = 0
    # 真正执行过的工具（TOOL_RESULT_END.state ∈ {success, error}）
    executed_tools: list[str] = field(default_factory=list)
    # 被权限拒绝的工具（TOOL_RESULT_END.state == denied）
    denied_tools: list[str] = field(default_factory=list)
    tokens: int = 0
    # 前缀缓存：命中量与写入量。用来观察「提示词布局改动是否毁掉前缀缓存」。
    cache_input_tokens: int = 0
    cache_creation_tokens: int = 0
    latency_ms: int = 0
    workspace_dir: Path | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CaseScore:
    """一条用例的判卷结果。

    `passed` 保留旧的严格语义（全部检查通过），用于历史可比；
    v3 的门禁看四元组：`safety_ok ∧ answer_ok ∧ trajectory_ok` 才算能力通过，
    引用/格式单独聚合。这样「答对了但没写行号」不会被记成「答错」。
    """

    passed: bool
    checks: list[Check]
    trajectory_score: float | None
    hard_failures: list[str] = field(default_factory=list)
    safety_ok: bool = True
    answer_ok: bool = True
    trajectory_ok: bool = True
    format_ok: bool = True
    attribution: str = "ok"
    attribution_detail: str = ""
