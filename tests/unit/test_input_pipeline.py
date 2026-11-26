"""输入解析管线单元测试：每个阶段的判定、计量与降级行为。"""

import urllib.error
from pathlib import Path

import pytest

from devops_agent import input as input_pkg
from devops_agent.config import InputPolicy, load_input
from devops_agent.input import pipeline as pipe
from devops_agent.input.attachments import Attachment
from devops_agent.input.pipeline.fetch import _guard_url


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


def test_failed_reference_reaches_model_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抓不到的引用必须写进上下文：只记在阶段详情里，模型会自己去重抓同一个地址。"""

    def boom(url: str) -> str:
        raise TimeoutError("timeout")

    monkeypatch.setattr(pipe, "_fetch_text", boom)
    prepared = pipe.prepare(
        "讲解 https://example.com/x", workspace_root=tmp_path, remote_fetch=True
    )
    assert "### 本轮未能取到的材料" in prepared.text
    assert "抓取失败：TimeoutError" in prepared.text
    assert "同一目标失败两次就停止尝试" in prepared.text
    compose = _stage(prepared, "compose")
    # 以前只拼成功材料，全失败等于原样透传 —— 模型完全不知道材料没拿到
    assert compose.status == "ok"
    assert "未取到 1 份" in compose.summary


def test_failed_attachment_reaches_model_context() -> None:
    """附件解析失败同样要说进上下文，否则模型会以为用户没给材料。"""
    broken = Attachment(id="a1", name="broken.pdf", size=10, suffix=".pdf", problem="解析失败")
    prepared = pipe.prepare("看附件", workspace_root=None, attachments=[broken])
    assert "broken.pdf → 解析失败" in prepared.text


def test_materials_and_misses_coexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """部分成功时两节都要在：拿到的是材料，没拿到的是清单。"""
    (tmp_path / "ok.md").write_text("有效内容", encoding="utf-8")

    def boom(url: str) -> str:
        raise TimeoutError("timeout")

    monkeypatch.setattr(pipe, "_fetch_text", boom)
    prepared = pipe.prepare(
        "看 ./ok.md 和 https://example.com/x", workspace_root=tmp_path, remote_fetch=True
    )
    assert "### 引用材料 1 · ./ok.md" in prepared.text
    assert "### 本轮未能取到的材料" in prepared.text
    assert prepared.text.index("引用材料 1") < prepared.text.index("本轮未能取到")
    assert "1 份材料 · 未取到 1 份" in _stage(prepared, "compose").summary


def test_resolved_input_has_no_miss_section(tmp_path: Path) -> None:
    """全部取到就不该冒出空的「未能取到」小节。"""
    (tmp_path / "ok.md").write_text("有效内容", encoding="utf-8")
    prepared = pipe.prepare("看 ./ok.md", workspace_root=tmp_path)
    assert "本轮未能取到的材料" not in prepared.text


def test_remote_fetch_off_is_not_reported_as_miss(tmp_path: Path) -> None:
    """远程抓取默认关闭不是「内容不可得」：不能写进上下文劝退模型自己取。"""
    prepared = pipe.prepare("看 https://example.com/x", workspace_root=tmp_path)
    assert "本轮未能取到的材料" not in prepared.text
    assert "远程抓取未开启" in _stage(prepared, "expand").detail


def test_expand_fetches_remote_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipe, "_fetch_text", lambda url: "远端正文")
    prepared = pipe.prepare(
        "总结 https://example.com/doc", workspace_root=tmp_path, remote_fetch=True
    )
    assert "远端正文" in prepared.text


def test_expand_remote_disabled_by_default(tmp_path: Path) -> None:
    """远程抓取关闭是「没尝试」，不是「失败」。

    回归：这条以前断言 status == "error" —— 于是默认配置下每轮都显示
    「1 条引用全部未取到」红灯，而模型那边又被刻意告知「什么都没缺」。
    """
    prepared = pipe.prepare("总结 https://example.com/doc", workspace_root=tmp_path)
    expand = _stage(prepared, "expand")
    assert expand.status == "skip"
    assert expand.summary == "未开启远程抓取，跳过 1 条 URL"
    assert expand.failed == 0
    assert prepared.degraded == 0
    assert "远程抓取未开启" in expand.detail


def test_expand_all_failures_still_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真尝试过的全失败仍然是 error：区分「没试」与「试了不行」。"""

    def boom(url: str) -> str:
        raise TimeoutError("timeout")

    monkeypatch.setattr(pipe, "_fetch_text", boom)
    prepared = pipe.prepare("看 https://example.com/a", workspace_root=tmp_path, remote_fetch=True)
    expand = _stage(prepared, "expand")
    assert expand.status == "error"
    assert expand.failed == 1
    assert expand.summary == "1 条引用全部未取到"


def test_expand_mixed_skip_and_success_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部分跳过只是附注：本地引用取到了，阶段就该是 ok。"""
    (tmp_path / "ok.md").write_text("有效内容", encoding="utf-8")
    prepared = pipe.prepare("看 ./ok.md 和 https://example.com/a", workspace_root=tmp_path)
    expand = _stage(prepared, "expand")
    assert expand.status == "ok"
    assert expand.summary == "1 条成功 · 1 条跳过"
    assert expand.failed == 0


def test_http_error_reports_status_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """失败原因要可行动：至少给出 HTTP 状态码，而不是只有异常类名。"""

    def boom(url: str) -> str:
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(pipe, "_fetch_text", boom)
    prepared = pipe.prepare("看 https://example.com/x", workspace_root=tmp_path, remote_fetch=True)
    expand = _stage(prepared, "expand")
    assert "HTTP 404" in expand.detail


def test_url_guard_rejects_loopback() -> None:
    """开启远程抓取也不该替谁访问本机端口：地址守卫按解析到的 IP 判断。"""
    with pytest.raises(ValueError, match="非公网地址"):
        _guard_url("http://127.0.0.1:8080/admin", InputPolicy())


def test_url_guard_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="http/https"):
        _guard_url("file:///etc/passwd", InputPolicy())


def test_url_guard_allows_private_when_explicitly_configured() -> None:
    _guard_url("http://127.0.0.1:8080/admin", InputPolicy(allow_private_addresses=True))


def test_detect_reports_truncation(tmp_path: Path) -> None:
    """静默截断会让用户以为全都读了：detail 必须写明还有几条没登记。"""
    text = " ".join(f"https://example.com/{i}" for i in range(pipe.MAX_REFERENCES + 2))
    prepared = pipe.prepare(text, workspace_root=tmp_path)
    detect = _stage(prepared, "detect")
    assert detect.items == pipe.MAX_REFERENCES
    assert "另有 2 条超出上限未登记" in detect.detail


def test_input_policy_matches_checked_in_yaml() -> None:
    """input.yaml 与 InputPolicy 的默认值必须一致：两边各写一份，靠这条对账防漂移。"""
    assert load_input() == InputPolicy()


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


def _attachment(name: str, text: str, problem: str = "") -> input_pkg.Attachment:
    return input_pkg.Attachment(
        id="a" * 16, name=name, size=len(text), suffix=".md", text=text, problem=problem
    )


def test_attach_skips_when_no_files() -> None:
    prepared = pipe.prepare("纯文本", workspace_root=None)
    stage = _stage(prepared, "attach")
    assert stage.status == "skip"
    assert stage.changed is False
    assert "未附加文件" == stage.summary


def test_attach_merges_file_text_into_context() -> None:
    prepared = pipe.prepare(
        "看下这份日志", workspace_root=None, attachments=[_attachment("app.log", "ERROR boom")]
    )
    attach = _stage(prepared, "attach")
    assert attach.items == 1
    assert attach.changed is True
    assert "1 份附件已读取" == attach.summary
    assert "ERROR boom" in prepared.text
    # 附件进入的是材料附录，而不是用户原话
    assert prepared.text.startswith("看下这份日志")


def test_attach_reports_failed_file_without_breaking_turn() -> None:
    prepared = pipe.prepare(
        "分析", workspace_root=None, attachments=[_attachment("blob.bin", "", "二进制文件")]
    )
    attach = _stage(prepared, "attach")
    assert attach.status == "error"
    assert attach.failed == 1
    assert prepared.degraded == 1
    assert "分析" in prepared.text


def test_attachment_text_is_redacted_before_model() -> None:
    """上传文件里的密钥同样不能进上下文——脱敏对正文与材料一视同仁。"""
    prepared = pipe.prepare(
        "看看配置",
        workspace_root=None,
        attachments=[_attachment(".env", "OPENAI=sk-abcdefghijklmnopqrstuvwxyz012345")],
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in prepared.text
    assert "{{secret:openai_key}}" in prepared.text
    assert _stage(prepared, "redact").items == 1


def test_secret_sink_receives_detected_value() -> None:
    captured: list[tuple[str, str]] = []
    pipe.prepare(
        "key=sk-abcdefghijklmnopqrstuvwxyz012345",
        workspace_root=None,
        on_secret=lambda name, value: captured.append((name, value)),
    )
    assert captured == [("openai_key", "sk-abcdefghijklmnopqrstuvwxyz012345")]


def test_different_secrets_get_distinct_placeholder_names() -> None:
    """两把不同的 key 不能共用一个占位符，否则注入时必然张冠李戴。"""
    first = "sk-" + "a" * 30
    second = "sk-" + "b" * 30
    prepared = pipe.prepare(f"{first} 和 {second}", workspace_root=None)
    assert "{{secret:openai_key}}" in prepared.text
    assert "{{secret:openai_key_2}}" in prepared.text


def test_same_secret_reuses_placeholder() -> None:
    secret = "sk-" + "c" * 30
    prepared = pipe.prepare(f"{secret} 再贴一次 {secret}", workspace_root=None)
    assert prepared.text.count("{{secret:openai_key}}") == 2
    assert "openai_key_2" not in prepared.text


def test_budget_trims_material_before_user_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """材料再多也不能把用户原话挤没：先按份额裁材料，正文最后才动。"""
    monkeypatch.setattr(pipe, "MAX_TOTAL_CHARS", 600)
    prepared = pipe.prepare(
        "一句话问题",
        workspace_root=None,
        # 每行都不同：重复行会被去噪折叠掉，就测不到「按份额裁剪」这条路径了
        attachments=[_attachment("big.log", "\n".join(f"第 {i} 行 内容" for i in range(400)))],
    )
    budget = _stage(prepared, "budget")
    assert budget.status == "ok"
    assert "材料裁剪" in budget.summary
    assert "一句话问题" in prepared.text
    assert len(prepared.text) < 2000


def test_budget_keeps_short_material_untouched() -> None:
    prepared = pipe.prepare("问题", workspace_root=None, attachments=[_attachment("a.md", "材料")])
    assert _stage(prepared, "budget").status == "skip"
