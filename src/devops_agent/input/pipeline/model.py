"""输入解析的数据模型与阶段辅助。

- 每个阶段产出一条 Stage 记录（before / after / 耗时 / 是否真的改动），
  网关按记录发事件并落库，前端据此把「这条输入被做了什么」画成轨迹；
- PreparedInput 是交给模型的最终文本 + 全过程记录。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

# token 估算口径：与 AgentScope formatter 的「字节数 / 4」同源，按字符算更保守
CHARS_PER_TOKEN: Final = 4

STAGE_LABELS: Final[dict[str, str]] = {
    "normalize": "归一化",
    "detect": "引用探测",
    "expand": "引用展开",
    "attach": "附件读取",
    "redact": "密钥脱敏",
    "budget": "预算裁剪",
    "compose": "组装",
}

KIND_LABELS: Final[dict[str, str]] = {
    "url": "URL",
    "path": "本地路径",
    "git": "git 引用",
    "file": "附件",
}


@dataclass(frozen=True, slots=True)
class Stage:
    """一个解析阶段的执行结果（前端轨迹的一行）。"""

    stage: str
    label: str
    status: str  # ok / skip / error
    summary: str
    before: int
    after: int
    items: int
    ms: int
    detail: str
    changed: bool
    failed: int = 0  # 该阶段内部失败的条目数（部分降级）


@dataclass(frozen=True, slots=True)
class Reference:
    """从输入里识别出的一条引用。"""

    kind: str  # url / path / git / file
    value: str


@dataclass(frozen=True, slots=True)
class PreparedInput:
    """解析结果：交给模型的最终文本 + 全过程记录。"""

    original: str
    text: str
    stages: tuple[Stage, ...]
    references: tuple[Reference, ...]
    redactions: int

    @property
    def degraded(self) -> int:
        """出错（非 skip）的阶段数，供轨迹标「降级」徽标。"""
        return sum(1 for stage in self.stages if stage.status == "error" or stage.failed)

    @property
    def effective(self) -> tuple[Stage, ...]:
        """真正改动了输入的阶段——界面只展示这些。"""
        return tuple(stage for stage in self.stages if stage.changed or stage.status == "error")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算，用于在轨迹里与模型回报的 input_tokens 对账。"""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _stage(
    name: str,
    *,
    status: str,
    summary: str,
    before: int,
    after: int,
    changed: bool,
    items: int = 0,
    ms: int = 0,
    detail: str = "",
    failed: int = 0,
) -> Stage:
    return Stage(
        stage=name,
        label=STAGE_LABELS[name],
        status=status,
        summary=summary,
        before=before,
        after=after,
        items=items,
        ms=ms,
        detail=detail,
        changed=changed,
        failed=failed,
    )
