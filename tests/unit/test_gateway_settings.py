"""网关设置的测试：基线缺失时的回落、环境变量读取时机、策略注入。

部署 yaml 已收敛成「代码默认 + config.db 覆盖」，仓库里不再存一份同值副本；
`policy.yaml` 仍是文件（安全域知识只能走 git 评审）。下面两条对账分别钉住
「缺 gateway 基线不影响装配」与「policy 改了要同步 ConfirmPolicy」。
"""

from __future__ import annotations

import pytest

from devops_agent.config import (
    ConfirmPolicy,
    GatewayPolicy,
    GatewayStreamPolicy,
    load_gateway,
    load_policy,
)
from devops_agent.gateway.app import app, create_app
from devops_agent.gateway.settings import DEFAULT_DOMAIN, ENV_DOMAIN, ENV_MODEL, Settings
from devops_agent.gateway.turn.ending import RunLimits
from devops_agent.runtime.runtime import EchoRuntime


def test_gateway_loads_after_deployment_yaml_removed() -> None:
    """gateway 基线已删：缺文件即全回落代码默认，装配行为不变。"""
    assert load_gateway() == GatewayPolicy()


def test_checked_in_policy_yaml_matches_confirm_defaults() -> None:
    """policy.yaml 的 confirm 段与 ConfirmPolicy 的默认值必须逐项相等。"""
    assert load_policy().confirm == ConfirmPolicy()


def test_env_values_are_read_at_request_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """domain/model 按请求读：/health 要反映当前进程环境，测试也逐个用例 patch 它。"""
    monkeypatch.setenv(ENV_DOMAIN, "delivery")
    monkeypatch.setenv(ENV_MODEL, "qwen-max")
    settings = Settings()
    assert settings.domain() == "delivery"
    assert settings.model() == "qwen-max"

    monkeypatch.delenv(ENV_DOMAIN)
    monkeypatch.delenv(ENV_MODEL)
    assert Settings().domain() == DEFAULT_DOMAIN
    assert Settings().model() == ""


def test_empty_domain_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """空串按缺省处理：空的域标签会让健康分分桶与 max_iters 取值都失真。"""
    monkeypatch.setenv(ENV_DOMAIN, "")
    assert Settings().domain() == DEFAULT_DOMAIN


def test_injected_policy_changes_behavior_without_monkeypatch() -> None:
    """策略注入是这套拆分的收益：改上限不用改模块常量，也不用重启配置缓存。"""
    settings = Settings(policy=GatewayPolicy(stream=GatewayStreamPolicy(ask_limit=1)))
    assert RunLimits.from_policy(settings.stream).ask_limit == 1
    # 默认实例不受影响（不可变快照，没有隐式共享）
    assert RunLimits.from_policy(Settings().stream).ask_limit == 3


def test_create_app_isolates_runtime_and_settings() -> None:
    """装配根注入：测试可以拿独立实例，不必再改模块全局 state。

    这条同时钉住「三处 state 键的名字」—— 既有测试与会话注册表的懒建逻辑都按它们取值。
    """
    runtime = EchoRuntime()
    built = create_app(runtime=runtime)

    assert built.state.runtime is runtime, "显式注入的运行时不能被 select_runtime 覆盖"
    assert built.state.settings.stream.ask_limit == 3, "设置随实例装配，来自 gateway.yaml"
    assert built.state.sessions is None, "注册表懒建：替换 runtime 后下一次请求才建工厂"
    assert built.state.sessions_base is None
    assert built is not app, "每次调用都是一个独立实例"
