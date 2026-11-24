"""输入侧能力：解析管线、附件存储与它们的事件契约（纯 stdlib，不依赖框架）。"""

from devops_agent.input.attachments import Attachment, AttachmentStore
from devops_agent.input.events import BLOCK_ID, end_event, stage_event, start_event
from devops_agent.input.pipeline import (
    MAX_REFERENCES,
    MAX_TOTAL_CHARS,
    PreparedInput,
    Reference,
    Stage,
    estimate_tokens,
    prepare,
)

__all__ = [
    "BLOCK_ID",
    "MAX_REFERENCES",
    "MAX_TOTAL_CHARS",
    "Attachment",
    "AttachmentStore",
    "PreparedInput",
    "Reference",
    "Stage",
    "end_event",
    "estimate_tokens",
    "prepare",
    "stage_event",
    "start_event",
]
