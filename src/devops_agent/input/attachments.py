"""附件存储与正文抽取 —— 让「拖进来的文件」和「写在话里的路径」走同一条管线。

设计取舍：
- 上传即抽正文并落盘，请求期只按 id 取回文本：对话轮次里不再读一次文件，
  也让「同一份附件重发」不需要重传（前端只带 id）。
- 抽取全部用 stdlib（OOXML 就是 zip + xml），PDF 用 pypdf 且在缺失时降级；
  不引入 markitdown 这类大依赖——它会拖进 pdfminer / mammoth 一整串，
  而这里要覆盖的只是「贴一份配置、日志、文档」的场景。
- 任何抽取失败都折成 `problem` 字段返回，不抛异常：一份打不开的附件
  只损失它自己，不损失这一轮对话。
- 文件名只保留 basename 并过滤控制字符，目录名用随机 id：
  上传接口收到的 name 完全不可信，拼路径前必须先归一。
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 单文件 10MB：再大就不是「贴一份材料」的场景，而且要防把磁盘当仓库镜像用
MAX_UPLOAD_BYTES: Final = 10 * 1024 * 1024
# 解压后总量上限：zip 的压缩比可达上千倍，只限压缩包体积挡不住 zip bomb
_MAX_UNPACKED_BYTES: Final = 32 * 1024 * 1024
# 流式读取的分块大小：边读边累加，不在判断前把整条条目读进内存
_UNPACK_CHUNK: Final = 64 * 1024
_UNPACK_LIMIT_PROBLEM: Final = "解压后体积超过上限，已中止"
# 单份附件交给管线的字符上限：总预算由管线兜底，这里只挡「单个文件吃满一轮」
MAX_ATTACH_CHARS: Final = 40_000
# 一轮最多挂 8 个附件，与解析管线的引用上限保持同一量级
MAX_ATTACHMENTS: Final = 8
# 附件只服务于「上传即可用」，过期清掉避免 workspace 无限膨胀
RETENTION_DAYS: Final = 7

ATTACHMENT_DIR_ENV: Final = "DEVOPS_AGENT_ATTACHMENT_DIR"

# OOXML 正文所在条目：docx 单文件、xlsx 是共享字符串、pptx 是多页
_OOXML_TARGET: Final[dict[str, str]] = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/sharedStrings.xml",
}
_OOXML_SLIDE_PREFIX: Final = "ppt/slides/slide"

# 目录名只接受自己生成的 16 位十六进制：否则 ../ 就能读到任意路径
_ID_RE: Final = re.compile(r"^[0-9a-f]{16}$")
# 段落/单元格关闭标签换成换行，避免整篇文档挤成一行
_BLOCK_BREAK_RE: Final = re.compile(r"</(?:w:p|a:p|si|row)>")
_TAG_RE: Final = re.compile(r"<[^>]+>")
_WS_RE: Final = re.compile(r"[ \t\u00a0]+")
_UNSAFE_NAME_RE: Final = re.compile(r"[^\w\-. ]+", re.UNICODE)
_ENTITIES: Final[tuple[tuple[str, str], ...]] = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&apos;", "'"),
    ("&amp;", "&"),
)


@dataclass(frozen=True, slots=True)
class Attachment:
    """一份上传材料：元信息 + 已抽好的正文（或取不到正文的原因）。"""

    id: str
    name: str
    size: int
    suffix: str
    text: str = ""
    problem: str = ""
    truncated: bool = False

    @property
    def chars(self) -> int:
        """正文字符数，供轨迹展示与预算估算。"""
        return len(self.text)

    @property
    def ok(self) -> bool:
        """是否取到了可交给模型的正文。"""
        return not self.problem


def sanitize_name(name: str) -> str:
    """把任意上传文件名折成安全的 basename：去掉目录、控制字符与超长部分。"""
    base = Path(name.replace("\\", "/")).name
    base = _UNSAFE_NAME_RE.sub("_", base).strip(" .")
    if not base:
        return "attachment"
    return base[:120]


def _decode_text(data: bytes) -> tuple[str, str]:
    """按 utf-8 解码；出现 NUL 就当二进制拒绝（文本文件不会有 NUL）。"""
    if b"\x00" in data[:2048]:
        return "", "二进制文件，暂不支持解析"
    return data.decode("utf-8", errors="replace"), ""


def _xml_to_text(xml: str) -> str:
    """剥标签取正文：先按段落断行，再去标签、还原实体、压缩空白。"""
    text = _BLOCK_BREAK_RE.sub("\n", xml)
    text = _TAG_RE.sub("", text)
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    lines = (_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _ooxml_entries(names: list[str], suffix: str) -> list[str]:
    """挑出正文所在的包内条目：pptx 是多页，docx/xlsx 各只有一个目标。"""
    if suffix == ".pptx":
        return sorted(name for name in names if name.startswith(_OOXML_SLIDE_PREFIX))
    target = _OOXML_TARGET.get(suffix)
    return [target] if target is not None and target in names else []


class _UnpackedTooLarge(Exception):
    """解压后体积超限的内部信号：只在抽取层流转，不对外抛。"""


def _read_entry(archive: zipfile.ZipFile, name: str, remaining: int) -> bytes:
    """限流读取一个包内条目；超过剩余预算立即中止。

    先看 zip 头声明的 `file_size`（省掉无谓的读取），再分块读并累加——
    头里的值可以被伪造成很小，所以两者都要。
    """
    info = archive.getinfo(name)
    if info.file_size > remaining:
        raise _UnpackedTooLarge
    chunks: list[bytes] = []
    read = 0
    with archive.open(info) as handle:
        while chunk := handle.read(_UNPACK_CHUNK):
            read += len(chunk)
            if read > remaining:
                raise _UnpackedTooLarge
            chunks.append(chunk)
    return b"".join(chunks)


def _unpack_parts(archive: zipfile.ZipFile, targets: list[str]) -> list[str]:
    """按跨条目共享的总预算解压正文：pptx 多页也不允许总量叠加绕开上限。"""
    remaining = _MAX_UNPACKED_BYTES
    parts: list[str] = []
    for name in targets:
        raw = _read_entry(archive, name, remaining)
        remaining -= len(raw)
        parts.append(_xml_to_text(raw.decode("utf-8", "replace")))
    return parts


def _ooxml_text(data: bytes, suffix: str) -> tuple[str, str]:
    """从 OOXML 包里抽正文：docx/xlsx/pptx 本质都是 zip + xml。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            targets = _ooxml_entries(archive.namelist(), suffix)
            if not targets:
                return "", "包内未找到正文条目"
            parts = _unpack_parts(archive, targets)
    except _UnpackedTooLarge:
        return "", _UNPACK_LIMIT_PROBLEM
    except (zipfile.BadZipFile, OSError):
        return "", "文件损坏，无法解压"
    text = "\n".join(part for part in parts if part)
    return (text, "") if text.strip() else ("", "未提取到正文")


def _pdf_text(data: bytes) -> tuple[str, str]:
    """PDF 抽正文：pypdf 已装就用，没装就明确告知而不是静默给空。

    用 importlib 而不是 `from pypdf import ...`：pypdf 是可选依赖，
    直接 import 会让 mypy 在未安装它的 CI 上报「找不到实现」。
    """
    try:
        module = importlib.import_module("pypdf")
    except ImportError:
        return "", "缺少 pypdf，PDF 无法解析"
    try:
        reader = module.PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pypdf 的异常类型随版本而变，统一降级
        return "", f"PDF 解析失败：{type(exc).__name__}"
    text = "\n".join(pages).strip()
    return (text, "") if text else ("", "PDF 没有可提取的文字层")


def extract_text(name: str, data: bytes) -> tuple[str, str]:
    """抽取正文，返回 (文本, 问题)；问题非空表示这份材料用不了。"""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(data)
    if suffix == ".pptx" or suffix in _OOXML_TARGET:
        return _ooxml_text(data, suffix)
    return _decode_text(data)


def default_root() -> Path:
    """附件目录：DEVOPS_AGENT_ATTACHMENT_DIR 优先，否则落在 gitignore 的 workspace/。"""
    override = os.environ.get(ATTACHMENT_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "workspace" / "attachments"


class AttachmentStore:
    """附件落盘：一 id 一目录，meta.json + content.txt + raw.bin。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root if root is not None else default_root()).resolve()

    def save(self, name: str, data: bytes) -> Attachment:
        """保存一次上传：超限抛 ValueError（由 API 转 413），抽取失败只记 problem。"""
        if len(data) > MAX_UPLOAD_BYTES:
            limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            raise ValueError(f"文件超过 {limit_mb}MB 上限")
        safe = sanitize_name(name)
        text, problem = extract_text(safe, data)
        truncated = len(text) > MAX_ATTACH_CHARS
        if truncated:
            text = text[:MAX_ATTACH_CHARS]
        attachment = Attachment(
            id=uuid.uuid4().hex[:16],
            name=safe,
            size=len(data),
            suffix=Path(safe).suffix.lower(),
            text=text,
            problem=problem,
            truncated=truncated,
        )
        folder = self.root / attachment.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "raw.bin").write_bytes(data)
        (folder / "content.txt").write_text(text, encoding="utf-8")
        (folder / "meta.json").write_text(
            json.dumps(
                {
                    "id": attachment.id,
                    "name": safe,
                    "size": attachment.size,
                    "suffix": attachment.suffix,
                    "problem": problem,
                    "truncated": truncated,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.prune()
        return attachment

    def load(self, ids: list[str]) -> list[Attachment]:
        """按 id 取回附件；取不到的补一条带 problem 的占位，轨迹里能看出少了什么。"""
        loaded: list[Attachment] = []
        for raw in ids[:MAX_ATTACHMENTS]:
            attachment = self._load_one(raw)
            loaded.append(
                attachment
                if attachment is not None
                else Attachment(id=raw, name=raw, size=0, suffix="", problem="附件不存在或已过期")
            )
        return loaded

    def _load_one(self, attachment_id: str) -> Attachment | None:
        if not _ID_RE.match(attachment_id):
            return None
        folder = self.root / attachment_id
        meta_path = folder / "meta.json"
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            text = (folder / "content.txt").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        return Attachment(
            id=attachment_id,
            name=str(meta.get("name", attachment_id)),
            size=int(meta.get("size", 0)),
            suffix=str(meta.get("suffix", "")),
            text=text,
            problem=str(meta.get("problem", "")),
            truncated=bool(meta.get("truncated", False)),
        )

    def delete(self, attachment_id: str) -> bool:
        """删除一份附件；id 不合规直接拒绝，不存在返回 False。"""
        if not _ID_RE.match(attachment_id):
            return False
        folder = self.root / attachment_id
        if not folder.is_dir():
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return True

    def prune(self, max_age_days: int = RETENTION_DAYS) -> int:
        """清理过期附件；失败不影响本次上传（磁盘问题不该让用户传不了文件）。"""
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for folder in self.root.iterdir():
            if not folder.is_dir() or not _ID_RE.match(folder.name):
                continue
            try:
                if folder.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
            except OSError:
                continue
        return removed
