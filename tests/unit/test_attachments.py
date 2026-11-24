"""附件：文件名归一、正文抽取、落盘与过期清理。

覆盖重点是「不可信输入」这条线——上传文件名和内容都是外部给的，
拼路径前必须归一，读不出来必须降级而不是抛异常。
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_agent.input import attachments as att
from devops_agent.input.attachments import Attachment, AttachmentStore, extract_text, sanitize_name
from devops_agent.main import app

client = TestClient(app)


def _docx(body: str) -> bytes:
    """最小可用的 docx：只要 word/document.xml 就能验证抽取链路。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f"<w:document><w:body><w:p><w:r><w:t>{body}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def _pptx(*slides: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for index, text in enumerate(slides, start=1):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                f"<p:sld><a:p><a:t>{text}</a:t></a:p></p:sld>",
            )
    return buf.getvalue()


def test_sanitize_name_strips_path_and_controls() -> None:
    """上传名完全不可信：路径分隔符与控制字符都不能进文件名。"""
    assert sanitize_name("../../etc/passwd") == "passwd"
    assert sanitize_name(r"C:\tmp\a b.log") == "a b.log"
    assert sanitize_name("a\x00b.txt") == "a_b.txt"
    assert sanitize_name("...") == "attachment"
    assert len(sanitize_name("x" * 400 + ".md")) <= 120


def test_extract_text_decodes_utf8() -> None:
    text, problem = extract_text("notes.md", "# 标题\n正文".encode())
    assert problem == ""
    assert "标题" in text


def test_extract_text_rejects_binary() -> None:
    text, problem = extract_text("blob.bin", b"\x00\x01\x02payload")
    assert text == ""
    assert "二进制" in problem


def test_extract_text_reads_docx() -> None:
    text, problem = extract_text("方案.docx", _docx("第一段"))
    assert problem == ""
    assert "第一段" in text


def test_extract_text_reads_pptx_pages_in_order() -> None:
    text, problem = extract_text("汇报.pptx", _pptx("封面", "结论"))
    assert problem == ""
    assert text.splitlines() == ["封面", "结论"]


def test_extract_text_reports_broken_office_package() -> None:
    text, problem = extract_text("坏.docx", b"not a zip at all")
    assert text == ""
    assert "损坏" in problem


def test_ooxml_rejects_unpacked_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    """压缩包很小但解压后超限：明确报超限，不抛异常也不返回正文。

    用高度可压缩的重复字节模拟 zip bomb，解压上限调小以把内存控制在 KB 级。
    """
    monkeypatch.setattr(att, "_MAX_UNPACKED_BYTES", 4096)
    text, problem = extract_text("bomb.docx", _docx("A" * 200_000))
    assert text == ""
    assert "解压后体积超过上限" in problem


def test_extract_text_without_pypdf_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf 是可选依赖：没装时要明确说缺什么，而不是返回空正文。"""

    def boom(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(att.importlib, "import_module", boom)
    text, problem = extract_text("spec.pdf", b"%PDF-1.4")
    assert text == ""
    assert "pypdf" in problem


def test_store_roundtrip(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    saved = store.save("readme.md", "内容".encode())
    loaded = store.load([saved.id])
    assert len(loaded) == 1
    assert loaded[0].text == "内容"
    assert loaded[0].name == "readme.md"
    assert loaded[0].ok
    # 原始字节也留一份，便于以后换抽取器重跑
    assert (tmp_path / saved.id / "raw.bin").is_file()


def test_store_truncates_long_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "MAX_ATTACH_CHARS", 5)
    saved = AttachmentStore(tmp_path).save("long.log", b"0123456789")
    assert saved.truncated
    assert saved.chars == 5


def test_store_rejects_oversized_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(att, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(ValueError, match="上限"):
        AttachmentStore(tmp_path).save("big.bin", b"12345")


def test_load_reports_missing_attachment(tmp_path: Path) -> None:
    """取不到的附件补一条带原因的占位：轨迹里要能看出「这里少了一份」。"""
    loaded = AttachmentStore(tmp_path).load(["0123456789abcdef", "../../etc/passwd"])
    assert len(loaded) == 2
    assert all(not item.ok for item in loaded)
    assert "不存在" in loaded[0].problem


def test_delete_only_accepts_generated_ids(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    saved = store.save("a.txt", b"x")
    assert store.delete("../../etc") is False
    assert store.delete(saved.id) is True
    assert store.delete(saved.id) is False


def test_prune_removes_expired_dirs(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    saved = store.save("old.txt", b"x")
    old = time.time() - 30 * 86400
    os.utime(tmp_path / saved.id, (old, old))
    assert store.prune(max_age_days=7) == 1
    assert not (tmp_path / saved.id).exists()


def test_prune_ignores_unrelated_entries(tmp_path: Path) -> None:
    (tmp_path / "keep").mkdir()
    store = AttachmentStore(tmp_path)
    assert store.prune(max_age_days=0) == 0


def test_api_upload_and_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(att.ATTACHMENT_DIR_ENV, str(tmp_path))
    resp = client.post(
        "/api/attachments", files={"file": ("logs/app.log", "line1\nline2", "text/plain")}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "app.log"
    assert body["status"] == "ok"
    assert body["chars"] == 11
    assert body["preview"].startswith("line1")

    deleted = client.delete(f"/api/attachments/{body['id']}")
    assert deleted.json() == {"deleted": True}


def test_api_upload_rejects_oversized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from devops_agent.input import api as att_api

    monkeypatch.setenv(att.ATTACHMENT_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(att, "MAX_UPLOAD_BYTES", 4)
    monkeypatch.setattr(att_api, "MAX_UPLOAD_BYTES", 4)
    resp = client.post("/api/attachments", files={"file": ("big.bin", b"12345678", "text/plain")})
    assert resp.status_code == 413


def test_api_upload_reports_unparsable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(att.ATTACHMENT_DIR_ENV, str(tmp_path))
    resp = client.post(
        "/api/attachments", files={"file": ("blob.bin", b"\x00\x01", "application/octet-stream")}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_attachment_chars_property() -> None:
    assert Attachment(id="a", name="a.txt", size=1, suffix=".txt", text="abc").chars == 3
