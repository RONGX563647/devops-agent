"""工作区配置域验收（SPEC-0021 A13）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devops_agent import config
from devops_agent.config_schema import WorkspacePolicy
from devops_agent.config_store import registry
from devops_agent.config_store.layers import yaml_layer


def test_domain_registered_and_takes_yaml_baseline() -> None:
    """workspace 域可读；yaml 段名是 workspaces；开与关由基线决定，模型默认仍为关。"""
    spec = registry.spec_of("workspace")
    assert spec.section == "workspaces"
    assert spec.model == "WorkspacePolicy"
    assert spec.hot == "restart"
    # 代码默认必须是关：灰度靠 baseline 打开，而不是把默认改成开
    assert WorkspacePolicy().enabled is False
    config.clear_cache()
    baseline = yaml_layer(spec)
    assert config.load_workspace().enabled is bool(baseline.get("enabled", False))


def test_out_of_range_values_rejected() -> None:
    """越界值在加载期就报错，而不是等请求期。"""
    with pytest.raises(ValidationError):
        WorkspacePolicy(max_workspaces=0)
    with pytest.raises(ValidationError):
        WorkspacePolicy(git_clone_timeout_sec=5)
    assert WorkspacePolicy().max_workspaces == 10


def test_env_overlay_including_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A13：env 覆盖生效，白名单按逗号拆成 list。"""
    monkeypatch.setenv("DEVOPS_AGENT_WORKSPACES_ENABLED", "1")
    monkeypatch.setenv("DEVOPS_AGENT_WORKSPACE_ALLOW_LOCAL", "true")
    monkeypatch.setenv("DEVOPS_AGENT_WORKSPACE_LOCAL_ROOTS", "/data/a, /data/b ,")
    overlay = registry.env_overlay("workspace")
    assert overlay["enabled"] is True
    assert overlay["allow_local_import"] is True
    assert overlay["allowed_local_roots"] == ["/data/a", "/data/b"]
    config.clear_cache()
    policy = config.load_workspace()
    assert policy.enabled is True
    assert policy.allowed_local_roots == ["/data/a", "/data/b"]


def test_bounds_exposed_for_panel() -> None:
    """面板能拿到数值边界。"""
    bounds = registry.bounds_of(registry.spec_of("workspace"))
    assert bounds["max_workspaces"]["min"] == 1
    assert bounds["max_workspaces"]["max"] == 200
