"""sandbox 配置域：默认值、校验、环境变量覆盖与注册表。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devops_agent.config import clear_cache, load_sandbox
from devops_agent.config_schema import (
    SandboxDockerPolicy,
    SandboxOpensandboxPolicy,
    SandboxPolicy,
)
from devops_agent.config_store.registry import BY_KEY, DOMAINS, defaults_of, env_overlay


def test_defaults_keep_local_mode() -> None:
    """默认 local：不改变现有开发/CI 行为，切容器必须显式部署。"""
    policy = SandboxPolicy()
    assert policy.mode == "local"
    assert policy.required is False
    assert policy.ttl_seconds == 900
    assert policy.docker.base_image == "python:3.11-slim"
    assert policy.docker.gateway_port == 5600
    assert policy.opensandbox.image == "python:3.11-slim"
    assert policy.opensandbox.protocol == "https"


def test_opensandbox_mode_is_accepted() -> None:
    policy = SandboxPolicy(mode="opensandbox")
    assert policy.mode == "opensandbox"


def test_opensandbox_rejects_empty_image() -> None:
    with pytest.raises(ValidationError):
        SandboxOpensandboxPolicy(image="")


def test_opensandbox_rejects_non_positive_timeouts() -> None:
    with pytest.raises(ValidationError):
        SandboxOpensandboxPolicy(request_timeout_seconds=0)
    with pytest.raises(ValidationError):
        SandboxOpensandboxPolicy(timeout_seconds=0)


def test_opensandbox_rejects_unknown_protocol() -> None:
    with pytest.raises(ValidationError):
        SandboxOpensandboxPolicy(protocol="ftp")


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate({"mode": "k8s"})


def test_non_positive_ttl_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy(ttl_seconds=0)


def test_empty_base_image_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxDockerPolicy(base_image="")


def test_docker_policy_mirrors_workspace_signature() -> None:
    docker = SandboxDockerPolicy(
        base_image="img:1",
        extra_pip=["pytest"],
        env={"K": "V"},
        gateway_port=9000,
    )
    assert docker.extra_pip == ["pytest"]
    assert docker.env == {"K": "V"}
    assert docker.gateway_port == 9000


def test_sandbox_domain_is_registered_as_restart() -> None:
    spec = BY_KEY["sandbox"]
    assert spec.hot == "restart"
    assert spec.model == "SandboxPolicy"
    assert spec in DOMAINS
    assert defaults_of(spec)["mode"] == "local"


def test_env_overlay_reads_mode_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_MODE", "docker")
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_REQUIRED", "1")
    assert env_overlay("sandbox") == {"mode": "docker", "required": True}


def test_env_overlay_ignores_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 env 值沿用旧语义：视为未设置，不把整份配置拦在门外。"""
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_MODE", "k8s")
    assert env_overlay("sandbox") == {}


def test_env_overlay_reads_opensandbox_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_MODE", "opensandbox")
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_OSB_DOMAIN", "osb.example.com")
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_OSB_IMAGE", "img:golden")
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_OSB_PROTOCOL", "http")
    assert env_overlay("sandbox") == {
        "mode": "opensandbox",
        "opensandbox.domain": "osb.example.com",
        "opensandbox.image": "img:golden",
        "opensandbox.protocol": "http",
    }


def test_env_overlay_ignores_unknown_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_OSB_PROTOCOL", "ftp")
    assert env_overlay("sandbox") == {}


def test_load_sandbox_merges_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_AGENT_SANDBOX_MODE", "auto")
    clear_cache()
    try:
        assert load_sandbox().mode == "auto"
    finally:
        clear_cache()
