"""输入准备的降级语义测试。

本模块的全部价值就是「失败即退化」：附件读不到、保险箱锁着、解析管线炸了，
都不能让这一轮对话失败。所以每条降级分支都必须有用例 —— 否则改着改着就会
有人把 `except` 删掉，而线上表现是「偶发对话失败」，很难反向定位到这里。
"""

from __future__ import annotations

from typing import Any

import pytest

from devops_agent.gateway import input_prep


class _BrokenStore:
    """模拟附件存储不可用。"""

    def load(self, ids: list[str]) -> list[Any]:
        raise RuntimeError("attachment store down")


class _BrokenVault:
    """模拟保险箱不可解锁。"""

    def put(self, name: str, value: str, *, source: str) -> None:
        raise RuntimeError("vault locked")


def test_attachment_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(input_prep, "AttachmentStore", _BrokenStore)
    assert input_prep.load_attachments(["a1"]) == []


def test_empty_attachment_list_never_touches_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise AssertionError("没有附件时不该访问存储")

    monkeypatch.setattr(input_prep, "AttachmentStore", _boom)
    assert input_prep.load_attachments([]) == []


def test_prepare_failure_keeps_original_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(input_prep, "prepare", _boom)
    assert input_prep.prepare_input("原始输入", []) is None


def test_redact_failure_falls_back_to_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_text: str) -> str:
        raise RuntimeError("redact exploded")

    monkeypatch.setattr(input_prep, "_redact_preview", _boom)
    assert input_prep.redact_for_record("原文") == "原文"


def test_vault_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """存不下凭据只是「下次还要问用户要」，不能让这一轮中断。"""
    monkeypatch.setattr(input_prep, "get_vault", _BrokenVault)
    assert input_prep.save_secret("api_key", "sk-x") is None
