"""附件 API —— 上传即抽取正文，只回元信息，不回正文（正文留在服务端由管线取用）。

为什么不让前端把正文再传回来：
- 正文可能就有几十 KB，回传给浏览器再由浏览器塞回请求，只是白白搬运；
- 正文一旦落到浏览器侧，用户输入框的「脱敏」边界就被绕开了——
  真正送给模型的材料必须经过服务端的解析管线，再脱敏、再裁预算。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from devops_agent.input.attachments import MAX_UPLOAD_BYTES, Attachment, AttachmentStore

router = APIRouter(prefix="/api", tags=["attachments"])

# 预览只给前若干个字符：够用户确认「传对了文件」，又不至于变成第二个正文通道
PREVIEW_CHARS = 240


class AttachmentOut(BaseModel):
    """上传结果：元信息 + 一小段预览。"""

    id: str
    name: str
    size: int
    suffix: str
    chars: int
    truncated: bool
    status: str
    problem: str = ""
    preview: str = ""


class DeleteOut(BaseModel):
    deleted: bool


def _to_out(attachment: Attachment) -> AttachmentOut:
    return AttachmentOut(
        id=attachment.id,
        name=attachment.name,
        size=attachment.size,
        suffix=attachment.suffix,
        chars=attachment.chars,
        truncated=attachment.truncated,
        status="ok" if attachment.ok else "error",
        problem=attachment.problem,
        preview=attachment.text[:PREVIEW_CHARS],
    )


@router.post("/attachments", response_model=AttachmentOut)
async def upload_attachment(file: Annotated[UploadFile, File()]) -> AttachmentOut:
    """接收一份文件：多读 1 字节用于判定超限，避免把超大文件整个读进内存。"""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        attachment = AttachmentStore().save(file.filename or "attachment", data)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return _to_out(attachment)


@router.delete("/attachments/{attachment_id}", response_model=DeleteOut)
def delete_attachment(attachment_id: str) -> DeleteOut:
    """删除一份附件（用户在上传后、发送前撤掉）。"""
    return DeleteOut(deleted=AttachmentStore().delete(attachment_id))
