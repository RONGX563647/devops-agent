"""输入解析管线单元测试：每个阶段的判定、计量与降级行为。"""

from pathlib import Path

import pytest

from devops_agent import input as input_pkg
from devops_agent.input import pipeline as pipe


def _stage(prepared: pipe.PreparedInput, name: str) -> pipe.Stage:
    return next(stage for stage in prepared.stages if stage.stage == name)


def test_normalize_reports_only_applied_edits() -> None:
    prepared = pipe.prepare("\ufeff第一行\r\n\r\n\r\n\r\n第二行  \n", workspace_root=None)
    stage = _stage(prepared, "normalize")
    assert stage.changed is True
    assert stage.before > stage.after
    assert "CRLF → LF" in stage.summary
    assert "BOM" in stage.summary
    assert "\r" not in prepared.text


def test_normalize_marks_clean_input_as_skip() -> None:
    prepared = pipe.prepare("已经干净的输入", workspace_root=None)
    stage = _stage(prepared, "normalize")
    assert stage.status == "skip"
    assert stage.changed is False


def test_detect_finds_url_path_and_git(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    text = "看 README.md 和 https://example.com/a 还有 git@github.com:o/r.git"
    prepared = pipe.prepare(text, workspace_root=tmp_path)
    stage = _stage(prepared, "detect")
    kinds = sorted(ref.kind for ref in prepared.references)
    assert kinds == ["git", "path", "url"]
    assert stage.items == 3
    assert "URL ×1" in stage.summary


def test_detect_respects_max_references() -> None:
    text = " ".join(f"https://example.com/{i}" for i in range(pipe.MAX_REFERENCES + 3))
    prepared = pipe.prepare(text, workspace_root=None)
    assert len(prepared.references) == pipe.MAX_REFERENCES


def test_bare_filename_requires_existing_file(tmp_path: Path) -> None:
    prepared = pipe.prepare("打开 missing.md 看看", workspace_root=tmp_path)
    assert _stage(prepared, "detect").status == "skip"


def test_expand_reads_local_file_into_appendix(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# 标题\n正文", encoding="utf-8")
    prepared = pipe.prepare("看 ./notes.md", workspace_root=tmp_path)
    expand = _stage(prepared, "expand")
    assert expand.status == "ok"
    assert expand.after > 0
    assert "引用材料 1 · ./notes.md" in prepared.text
    assert "正文" in prepared.text


def test_expand_rejects_path_outside_workspace(tmp_path: Path) -> None:
    prepared = pipe.prepare("看 ../etc/passwd", workspace_root=tmp_path / "root")
    expand = _stage(prepared, "expand")
    assert expand.status == "error"
    assert "越界" in expand.detail


def test_expand_skips_missing_and_binary_file(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    prepared = pipe.prepare("看 ./blob.bin 和 ./none.md", workspace_root=tmp_path)
    expand = _stage(prepared, "expand")
    assert expand.status == "error"
    assert "二进制" in expand.detail
    assert "不存在" in expand.detail


def test_expand_reports_partial_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ok.md").write_text("有效内容", encoding="utf-8")

    def boom(url: str) -> str:
        raise TimeoutError("timeout")

    monkeypatch.setattr(pipe, "_fetch_text", boom)
    prepared = pipe.prepare(
        "看 ./ok.md 和 https://example.com/x", workspace_root=tmp_path, remote_fetch=True
    )
    expand = _stage(prepared, "expand")
    assert expand.status == "ok"
    assert expand.failed == 1
    assert prepared.degraded == 1  # 部分降级仍要进「降级」计数
    assert "1 条成功 · 1 条降级" == expand.summary


def test_expand_fetches_remote_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipe, "_fetch_text", lambda url: "远端正文")
    prepared = pipe.prepare(
        "总结 https://example.com/doc", workspace_root=tmp_path, remote_fetch=True
    )
    assert "远端正文" in prepared.text


def test_expand_remote_disabled_by_default(tmp_path: Path) -> None:
    prepared = pipe.prepare("总结 https://example.com/doc", workspace_root=tmp_path)
    expand = _stage(prepared, "expand")
    assert expand.status == "error"
    assert "远程抓取未开启" in expand.detail


def test_redact_replaces_secrets_in_text_and_blocks(tmp_path: Path) -> None:
    (tmp_path / "env.md").write_text("token=ghp_abcdefghijklmnopqrstuvwxyz0123", encoding="utf-8")
    text = "key=sk-abcdefghijklmnopqrstuvwxyz012345 和 AKIAABCDEFGHIJKLMNOP"
    prepared = pipe.prepare(text, workspace_root=tmp_path)
    stage = _stage(prepared, "redact")
    assert stage.changed is True
    assert stage.items >= 2
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in prepared.text
    assert "AKIAABCDEFGHIJKLMNOP" not in prepared.text
    assert "{{secret:openai_key}}" in prepared.text
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123" not in prepared.text


def test_redact_keeps_short_values_untouched() -> None:
    prepared = pipe.prepare("token=abc", workspace_root=None)
    assert _stage(prepared, "redact").status == "skip"


def test_budget_trims_long_input_and_drops_noise() -> None:
    noise = "A" * 300  # 模拟 base64 块
    repeated = "\n".join(["同一行日志"] * 50)
    text = f"{repeated}\n{noise}\n" + ("内容" * 20_000)
    prepared = pipe.prepare(text, workspace_root=None)
    stage = _stage(prepared, "budget")
    assert stage.status == "ok"
    assert stage.after <= pipe.MAX_TOTAL_CHARS
    assert "base64" in stage.summary
    assert "重复行" in stage.summary
    assert "已截断" in prepared.text


def test_budget_skips_short_input() -> None:
    prepared = pipe.prepare("短输入", workspace_root=None)
    assert _stage(prepared, "budget").status == "skip"


def test_compose_passthrough_without_blocks() -> None:
    prepared = pipe.prepare("纯文本问题", workspace_root=None)
    compose = _stage(prepared, "compose")
    assert compose.status == "skip"
    assert prepared.text == "纯文本问题"


def test_effective_only_keeps_useful_stages() -> None:
    prepared = pipe.prepare("纯文本问题", workspace_root=None)
    assert [stage.stage for stage in prepared.effective] == []


def test_prepare_never_raises_on_empty_input() -> None:
    prepared = pipe.prepare("", workspace_root=None)
    assert prepared.text == ""
    assert len(prepared.stages) == len(pipe.STAGE_LABELS)


def test_html_to_text_falls_back_without_trafilatura() -> None:
    assert "标题" in pipe._html_to_text("<html><body><h1>标题</h1></body></html>")


def test_estimate_tokens_is_positive_for_empty() -> None:
    assert pipe.estimate_tokens("") == 1
    assert pipe.estimate_tokens("abcdefgh") == 2


def test_package_reexports() -> None:
    assert input_pkg.prepare is pipe.prepare
    assert callable(input_pkg.stage_event)
