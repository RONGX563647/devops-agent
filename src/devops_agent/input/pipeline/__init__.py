"""输入解析管线 —— 把用户原始输入整理成可安全交给模型的上下文。

拆分为子模块后的门面：对外仍以一个模块暴露 `prepare` 与全部模型/常量，
`from devops_agent.input import pipeline as pipe` 及逐个名字的 import 都继续可用，
测试对 `pipeline._fetch_text` / `pipeline.MAX_TOTAL_CHARS` 的 monkeypatch 也照旧生效。

设计取舍：
- 纯 stdlib、不 import 任何框架：网关（gateway/）与测试都能直接用；
- 每个阶段都产出一条 Stage 记录（before / after / 耗时 / 是否真的改动），
  网关按记录发事件并落库，前端据此把「这条输入被做了什么」画成轨迹；
- 阶段失败一律降级成该阶段的 status=error / skip，绝不抛异常打断对话：
  引用取不到（404、越界、二进制）只损失那一条材料，不损失这一轮。
- 「是否真的改动了输入」由 changed 表达：数据库全量保留（审计需要），
  界面只呈现 changed 或出错的阶段，避免一屏「无需处理」。

依赖策略（AGENTS.md §3.4：能力用内置、不重复造轮子）：
本地文件读取只用 stdlib；网页正文抽取优先用 trafilatura，未安装时退化为
标签剥离的兜底实现；远程抓取默认关闭，需 DEVOPS_AGENT_ALLOW_REMOTE_FETCH=1 显式开启。
"""

from __future__ import annotations

from .budget import MATERIAL_SHARE, MAX_TOTAL_CHARS, MIN_MATERIAL_CHARS
from .detect import MAX_REFERENCES
from .fetch import (
    FETCH_MAX_BYTES,
    FETCH_TIMEOUT_SECONDS,
    MAX_FILE_BYTES,
    REMOTE_FETCH_OFF,
    _fetch_text,
    _html_to_text,
)
from .model import (
    CHARS_PER_TOKEN,
    KIND_LABELS,
    STAGE_LABELS,
    PreparedInput,
    Reference,
    Stage,
    estimate_tokens,
)
from .prepare import default_workspace_root, prepare
from .redact import _PLACEHOLDER_RE, SecretSink, redact_preview

__all__ = [
    "CHARS_PER_TOKEN",
    "FETCH_MAX_BYTES",
    "FETCH_TIMEOUT_SECONDS",
    "KIND_LABELS",
    "MATERIAL_SHARE",
    "MAX_FILE_BYTES",
    "MAX_REFERENCES",
    "MAX_TOTAL_CHARS",
    "MIN_MATERIAL_CHARS",
    "REMOTE_FETCH_OFF",
    "STAGE_LABELS",
    "PreparedInput",
    "Reference",
    "SecretSink",
    "Stage",
    "_PLACEHOLDER_RE",
    "_fetch_text",
    "_html_to_text",
    "default_workspace_root",
    "estimate_tokens",
    "prepare",
    "redact_preview",
]
