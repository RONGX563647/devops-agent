"""运行时选择逻辑：env 决定真实模型还是 Echo。"""

from devops_agent.gateway.app import select_runtime
from devops_agent.runtime.runtime import EchoRuntime
from devops_agent.runtime.scope_runtime import AgentScopeRuntime


def test_select_runtime_echo_without_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DEVOPS_AGENT_API_KEY", raising=False)
    assert isinstance(select_runtime(), EchoRuntime)


def test_select_runtime_agentscope_with_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEVOPS_AGENT_API_KEY", "sk-test")
    monkeypatch.setenv("DEVOPS_AGENT_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("DEVOPS_AGENT_MODEL", "qwen-test")
    rt = select_runtime()
    assert isinstance(rt, AgentScopeRuntime)
