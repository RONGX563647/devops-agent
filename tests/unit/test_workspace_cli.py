"""工作区 CLI（SPEC-0021 §10.3）—— 与服务层共用同一组函数。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devops_agent.config_schema import WorkspacePolicy
from devops_agent.workspace import __main__ as cli
from devops_agent.workspace.service import WorkspaceService


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
) -> tuple[int, str]:
    service = WorkspaceService(
        WorkspacePolicy(
            enabled=True,
            allow_local_import=True,
            allowed_local_roots=[str(tmp_path)],
        ),
        db_path=tmp_path / "obs.db",
    )
    monkeypatch.setattr(cli, "_service", lambda: service)
    return cli.main(argv), ""


def test_cli_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """create → list → bind → status → archive → delete 全链路。"""
    repo = tmp_path / "code"
    repo.mkdir()
    code, _ = _run(monkeypatch, tmp_path, ["create", "--name", "demo", "--local", str(repo)])
    assert code == 0
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "demo"

    assert _run(monkeypatch, tmp_path, ["list"])[0] == 0
    assert json.loads(capsys.readouterr().out)

    assert _run(monkeypatch, tmp_path, ["bind", created["id"], "--session", "s1"])[0] == 0
    assert json.loads(capsys.readouterr().out)["workspace_id"] == created["id"]

    assert _run(monkeypatch, tmp_path, ["status", created["id"]])[0] == 0
    assert json.loads(capsys.readouterr().out)["session_count"] == 1

    assert _run(monkeypatch, tmp_path, ["archive", created["id"]])[0] == 0
    assert json.loads(capsys.readouterr().out)["status"] == "archived"

    assert _run(monkeypatch, tmp_path, ["delete", created["id"], "--force"])[0] == 0
    assert json.loads(capsys.readouterr().out)["deleted"] is True


def test_cli_requires_exactly_one_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--local 与 --git 必须二选一，否则退出码非零。"""
    code, _ = _run(monkeypatch, tmp_path, ["create", "--name", "x"])
    assert code == 2
    assert "workspace_source_invalid" in capsys.readouterr().err


def test_cli_reports_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """领域错误走 stderr 且退出码 2。"""
    code, _ = _run(monkeypatch, tmp_path, ["status", "ws_missing"])
    assert code == 2
    assert "workspace_not_found" in capsys.readouterr().err
