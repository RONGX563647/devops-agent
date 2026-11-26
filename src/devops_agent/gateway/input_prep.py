"""输入准备：把「用户原文 + 附件」变成「进模型的 text」。

唯一约束是**解析层出任何意外都不能阻断对话**，所以每个环节都失败即退化：
附件读不到当作没有、保险箱锁着只脱敏不保存、解析炸了退回原始输入。
只调 input 包的公开门面（`prepare` / `redact_preview`），不碰下划线名字。
"""

from __future__ import annotations

from devops_agent.input.attachments import Attachment, AttachmentStore
from devops_agent.input.pipeline import PreparedInput, prepare
from devops_agent.input.pipeline import redact_preview as _redact_preview
from devops_agent.secrets.vault import get_vault


def load_attachments(ids: list[str]) -> list[Attachment]:
    """按 id 取回附件正文；读取失败一律降级成空列表，不阻断这一轮。"""
    if not ids:
        return []
    try:
        return AttachmentStore().load(ids)
    except Exception:  # noqa: BLE001 - 附件是增强项，不是前置条件
        return []


def save_secret(name: str, value: str) -> None:
    """把解析阶段认出的凭据加密入库；保险箱不可用时只脱敏、不保存。"""
    try:
        get_vault().put(name, value, source="input")
    except Exception:  # noqa: BLE001 - 存不下也不能让对话失败
        return


def redact_for_record(user_input: str) -> str:
    """给观测库记录用的脱敏正文；不落保险箱，凭据落库由管线的 on_secret 负责。

    `/run/stream` 必须在解析管线之前 `recorder.start()`（`consume` 要求 run_id 已存在），
    所以那时拿不到管线的脱敏结果。这里用管线暴露的同一个纯函数提前脱一遍：
    它对已有占位符幂等，第二次不会再脱、也不会重复落库。
    失败时退回原文 —— 这是既有管线的降级语义，不让脱敏阻断对话。
    """
    try:
        return _redact_preview(user_input)
    except Exception:  # noqa: BLE001 - 脱敏失败退回原文，与管线降级语义一致
        return user_input


def prepare_input(user_input: str, attachments: list[Attachment]) -> PreparedInput | None:
    """解析输入；任何意外都退回 None（调用方沿用原始输入），不让解析层阻断对话。"""
    try:
        return prepare(user_input, attachments=attachments, on_secret=save_secret)
    except Exception:  # noqa: BLE001 - 解析失败退回原始输入
        return None
