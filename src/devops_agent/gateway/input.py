"""输入准备：附件读取、凭据识别与解析降级。

解析层出任何意外都不能阻断对话，因此这里全部是「失败即退化」的薄封装。
"""

from __future__ import annotations

from devops_agent.input.attachments import Attachment, AttachmentStore
from devops_agent.input.pipeline import PreparedInput, prepare
from devops_agent.secrets.vault import get_vault


def _load_attachments(ids: list[str]) -> list[Attachment]:
    """按 id 取回附件正文；读取失败一律降级成空列表，不阻断这一轮。"""
    if not ids:
        return []
    try:
        return AttachmentStore().load(ids)
    except Exception:
        return []


def _save_secret(name: str, value: str) -> None:
    """把解析阶段认出的凭据加密入库；保险箱不可用时只脱敏、不保存。"""
    try:
        get_vault().put(name, value, source="input")
    except Exception:
        return


def _prepare_input(user_input: str, attachments: list[Attachment]) -> PreparedInput | None:
    """解析输入；任何意外都退回 None（沿用原始输入），不让解析层阻断对话。"""
    try:
        return prepare(user_input, attachments=attachments, on_secret=_save_secret)
    except Exception:
        return None
