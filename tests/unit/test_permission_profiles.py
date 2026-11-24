"""权限档位测试：档位定义、可用集、档位→权限上下文 / 工具集的映射。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agentscope.permission import PermissionContext, PermissionMode

from devops_agent.governance.policy import build_permission_context
from devops_agent.governance.profiles import (
    AVAILABLE_ENV,
    DEFAULT_ENV,
    PROFILES,
    UnknownProfile,
    available,
    default_profile,
    resolve,
)
from devops_agent.tools.catalog import (
    READONLY_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    build_toolkit_for,
)


def test_profiles_bind_mode_tools_and_fallback() -> None:
    """档位是「mode + 工具集 + 兜底动作」的三元组，缺一项就不成档位。"""
    assert PROFILES["readonly"].mode == "explore"
    assert PROFILES["readonly"].fallback == "deny"
    assert PROFILES["workspace"].mode == "accept_edits"
    assert PROFILES["workspace"].fallback == "ask"
    assert PROFILES["auto"].mode == "dont_ask"
    assert PROFILES["unrestricted"].fallback == "hard_deny"
    # 只有放开档带写工具：其它档位切过去不该突然能改文件
    assert set(PROFILES["unrestricted"].tools) - set(READONLY_TOOL_NAMES) == set(WRITE_TOOL_NAMES)
    for key in ("readonly", "workspace", "execute", "auto"):
        assert "write" not in PROFILES[key].tools


def test_available_respects_env_and_skips_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置写错名字时跳过而不是让服务起不来。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly, bogus ,workspace")
    assert [item.key for item in available()] == ["readonly", "workspace"]


def test_available_defaults_without_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AVAILABLE_ENV, raising=False)
    keys = [item.key for item in available()]
    assert "unrestricted" not in keys
    assert "workspace" in keys


def test_default_profile_falls_back_when_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认档被配成未开放档时必须回落，而不是把一个越权档当默认。"""
    monkeypatch.setenv(AVAILABLE_ENV, "readonly")
    monkeypatch.setenv(DEFAULT_ENV, "unrestricted")
    assert default_profile().key == "readonly"


def test_resolve_rejects_unknown_and_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AVAILABLE_ENV, "readonly,workspace")
    assert resolve(None).key == "workspace"
    assert resolve("readonly").key == "readonly"
    with pytest.raises(UnknownProfile):
        resolve("unrestricted")  # 存在但未开放
    with pytest.raises(UnknownProfile):
        resolve("nope")


def test_context_for_workspace_has_no_hard_deny() -> None:
    """profile 专属 deny 规则只服务 hard_deny 档，不该影响正常档位。"""
    context = build_permission_context(profile=PROFILES["workspace"])
    assert context.mode == PermissionMode.ACCEPT_EDITS
    assert context.deny_rules == {}


def test_context_for_unrestricted_carries_profile_deny_rules() -> None:
    context = build_permission_context(profile=PROFILES["unrestricted"])
    assert context.mode == PermissionMode.BYPASS
    contents = [rule.rule_content for rule in context.deny_rules.get("Bash", [])]
    assert ".git/" in contents


def test_context_for_execute_injects_command_allowlist() -> None:
    context = build_permission_context(profile=PROFILES["execute"])
    assert context.mode == PermissionMode.ACCEPT_EDITS
    prefixes = [rule.rule_content for rule in context.allow_rules.get("Bash", [])]
    assert any(str(prefix).endswith(":*") for prefix in prefixes)
    assert "pytest:*" in prefixes


def test_context_without_profile_still_reads_yaml() -> None:
    """不带档位时保持旧行为（脚本 / 单测直接建引擎的场景）。"""
    context: PermissionContext = build_permission_context()
    assert context.mode == PermissionMode.DONT_ASK


def test_build_toolkit_for_only_writes_in_unrestricted(tmp_path: Path) -> None:
    def registered(names: tuple[str, ...]) -> set[str]:
        toolkit = build_toolkit_for(names, tmp_path)
        return {asyncio.run(toolkit.get_tool(name.capitalize())).name for name in names}

    assert registered(PROFILES["readonly"].tools) == {"Bash", "Read", "Glob", "Grep"}
    assert {"Write", "Edit"} <= registered(PROFILES["unrestricted"].tools)


def test_build_toolkit_for_rejects_unknown_tool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知工具"):
        build_toolkit_for(("bash", "teleport"), tmp_path)


def test_build_toolkit_for_passes_middlewares_to_every_tool(tmp_path: Path) -> None:
    """密钥注入挂在每个 ToolBase 上：漏挂一个就有一条绕过注入的路。"""
    middleware = MagicMock()
    names = READONLY_TOOL_NAMES + WRITE_TOOL_NAMES
    toolkit = build_toolkit_for(names, tmp_path, [middleware])
    for name in names:
        tool = asyncio.run(toolkit.get_tool(name.capitalize()))
        assert middleware in tool._middlewares, name
