"""工具外部依赖预检单测。

Grep 通过 backend 直接 spawn `rg`，不走 shell，所以「缺 rg」在运行时表现为
`ripgrep error (code 127)` 这种看不懂的报错。预检的职责就是把这层翻译成
「哪个工具、缺哪个可执行文件、怎么装」。
"""

import shutil
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devops_agent.gateway.app import app
from devops_agent.tools import preflight


def test_check_binary_returns_path_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert preflight.check_binary("rg") == "/usr/bin/rg"


def test_check_binary_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert preflight.check_binary("rg") is None


def test_readiness_marks_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "check_binary", lambda name: None)

    items = preflight.tool_readiness()
    assert [item.tool for item in items] == ["grep"]
    grep = items[0]
    assert grep.binary == "rg"
    assert grep.available is False
    assert grep.path is None
    # 提示必须是一条能直接粘进终端的命令，不能只说「请安装」
    assert grep.install_hint.strip()
    assert "ripgrep" in grep.install_hint.lower() or "rg" in grep.install_hint


def test_readiness_marks_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "check_binary", lambda name: "/opt/bin/rg")

    grep = preflight.tool_readiness()[0]
    assert grep.available is True
    assert grep.path == "/opt/bin/rg"
    assert grep.as_dict()["available"] is True


def test_missing_tools_reflects_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "check_binary", lambda name: None)
    assert preflight.missing_tools() == ["grep"]

    monkeypatch.setattr(preflight, "check_binary", lambda name: "/opt/bin/rg")
    assert preflight.missing_tools() == []


def test_readiness_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "check_binary", lambda name: None)

    payload = preflight.readiness_payload()
    assert payload["ok"] is False
    assert payload["missing"] == ["grep"]
    assert payload["tools"][0]["tool"] == "grep"


def test_health_payload_is_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/health` 只回答「哪个工具、缺什么」，不放安装提示那种长文本。"""
    monkeypatch.setattr(preflight, "check_binary", lambda name: None)

    payload: dict[str, Any] = preflight.health_payload()
    assert payload["tools"] == {"grep": {"available": False, "binary": "rg"}}
    # 内容安全链也要自述：缺 [sanitize] extra 时靠这段而不是靠猜
    assert set(payload["sanitize"]) == {"sanitizer", "extractor", "degraded", "reason"}


def test_install_hint_falls_back_for_unknown_binary() -> None:
    """没登记安装方式的依赖也要给出可用提示，不能是空串。"""
    hint = preflight._hint_for("frobnicator")
    assert "frobnicator" in hint
    assert "PATH" in hint


def test_install_hint_falls_back_for_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """登记表只覆盖 nt/posix；其他平台（未来可能出现的）给通用说法。"""
    monkeypatch.setattr(preflight.os, "name", "java")
    hint = preflight._hint_for("rg")
    assert "rg" in hint
    assert "PATH" in hint


def test_health_endpoint_reports_tool_readiness() -> None:
    """缺依赖不能让 /health 变成 5xx —— 探针会因此反复重启容器。"""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["tools"]["grep"]["binary"] == "rg"
    # 本机装没装 rg 不影响断言，只要求这个字段是布尔值
    assert isinstance(body["tools"]["grep"]["available"], bool)


def test_preflight_endpoint_exposes_install_hint() -> None:
    client = TestClient(app)
    resp = client.get("/api/tools/preflight")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "tools", "missing"}
    assert body["tools"][0]["tool"] == "grep"
    assert body["tools"][0]["install_hint"]
    assert body["missing"] == ([] if body["tools"][0]["available"] else ["grep"])
