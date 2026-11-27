"""配置加载 —— 强类型对象的唯一入口。

三层分工，方向上不允许成环：

- `config_schema.py`：pydantic 模型与内置默认值（纯类型，无 IO）；
- `config_store`：四层合流（代码默认 ← yaml 基线 ← config.db 覆盖 ← 环境变量）；
- 本文件：把合流后的 dict 变成模型，并对纯 yaml 的三类值（policy / gates / model）
  保留直接读取。

`policy` / `gates` / `model` 仍是纯 yaml：前者属于「安全域知识」、中间那份是
「CI 构建期契约」、后者有自己一套落盘实现（`model_settings/`）。它们不进配置库的
理由见 ADR-0005。
"""

from __future__ import annotations

from functools import cache

# 显式再导出（`as` 同名）：模型已下沉到 config_schema，但旧代码与测试都从
# `devops_agent.config` 取这些名字，改名只该改 import 源、不该改调用方。
from devops_agent.config_files import CONFIG_DIR_ENV as CONFIG_DIR_ENV
from devops_agent.config_files import config_dir as config_dir
from devops_agent.config_files import read_yaml
from devops_agent.config_schema import ConfirmPolicy as ConfirmPolicy
from devops_agent.config_schema import GatewayPolicy as GatewayPolicy
from devops_agent.config_schema import GatewaySessionsPolicy as GatewaySessionsPolicy
from devops_agent.config_schema import GatewayStreamPolicy as GatewayStreamPolicy
from devops_agent.config_schema import HealthPolicy as HealthPolicy
from devops_agent.config_schema import HealthWeights as HealthWeights
from devops_agent.config_schema import InputPolicy as InputPolicy
from devops_agent.config_schema import ModelSettings as ModelSettings
from devops_agent.config_schema import QualityGates as QualityGates
from devops_agent.config_schema import RiskPolicy as RiskPolicy
from devops_agent.config_schema import RulePolicy as RulePolicy
from devops_agent.config_schema import RuntimePolicy as RuntimePolicy
from devops_agent.config_schema import SandboxDockerPolicy as SandboxDockerPolicy
from devops_agent.config_schema import SandboxPolicy as SandboxPolicy
from devops_agent.config_schema import WorkspacePolicy as WorkspacePolicy
from devops_agent.config_store import store
from devops_agent.memory_policy import MemoryPolicy as MemoryPolicy

# 兼容旧名：既有代码与测试都通过 `config._load_yaml` / `config.config_dir` 取配置。
_load_yaml = read_yaml

# 权限（档位、工具集、builtin 规则）不在这里读配置：它是代码契约，声明在
# `governance/model.py`，编译在 `governance/compile.py`。同一份可用档位曾在
# policy.yaml 与代码里各写一遍，改一处忘另一处就会出现「前端列出的档位不是真正
# 开放的档位」——这类重复已随该次重构删除。


@cache
def load_policy() -> RuntimePolicy:
    """加载并缓存运行时策略（policy.yaml 的 confirm 段）。"""
    return RuntimePolicy.model_validate(_load_yaml("policy.yaml"))


@cache
def load_gates() -> QualityGates:
    """加载并缓存 CI 质量阈值。"""
    data = _load_yaml("gates.yaml")
    return QualityGates.model_validate(data.get("quality_gates", data))


def load_health() -> HealthPolicy:
    """健康分口径：代码默认 ← config.db 覆盖 ← 环境变量。

    不在这里 @cache：存取层自带 TTL 缓存，函数级缓存会让面板改完仍算旧口径。
    """
    return HealthPolicy.model_validate(store.effective("health"))


@cache
def load_model() -> ModelSettings:
    """加载并缓存模型参数默认值（model.yaml 的 model 段）。

    文件缺失时返回内置默认（与 `load_policy` 同样的降级策略）：
    写错配置不该让服务起不来，只是全部回落到默认值。
    """
    data = _load_yaml("model.yaml")
    return ModelSettings.model_validate(data.get("model", data))


@cache
def load_gateway() -> GatewayPolicy:
    """网关策略：代码默认 ← config.db 覆盖 ← 环境变量。

    仍保留 @cache：装配期读一次就固定（`Settings.load()`），改完要重启进程 ——
    与其假装热更新，不如让界面明说「重启后生效」。
    """
    return GatewayPolicy.model_validate(store.effective("gateway"))


def load_input() -> InputPolicy:
    """输入管线策略：代码默认 ← config.db 覆盖 ← 环境变量。

    每次抓取都读（存取层有 TTL 缓存），所以面板改完立即生效。
    """
    return InputPolicy.model_validate(store.effective("input"))


@cache
def load_sandbox() -> SandboxPolicy:
    """沙箱执行面配置：代码默认 ← config.db 覆盖 ← 环境变量。

    与 `load_gateway` 一样在装配期读一次就固定（`hot=restart`）：执行模式在
    进程生命周期内不能变，否则会出现「半进程用容器、半进程用宿主」。
    """
    return SandboxPolicy.model_validate(store.effective("sandbox"))


def load_memory() -> MemoryPolicy:
    """记忆策略：代码默认 ← config.db 覆盖 ← 环境变量。

    环境变量（enabled / backend / scope / user_id）由存取层的白名单合并，
    不再在这里二次覆盖 —— 两处都写会让优先级说不清。装配时读取，下一轮生效。
    """
    return MemoryPolicy.model_validate(store.effective("memory"))


@cache
def load_workspace() -> WorkspacePolicy:
    """工作区策略：代码默认 ← config.db 覆盖 ← 环境变量。

    与 gateway 域同样在装配期固定（根目录与白名单启动期就定），改完需重启；
    缓存让「每轮都读一次策略」不产生额外 IO。
    """
    return WorkspacePolicy.model_validate(store.effective("workspace"))


def clear_cache() -> None:
    """测试中切换配置目录 / 库路径后调用。"""
    load_policy.cache_clear()
    load_gates.cache_clear()
    load_model.cache_clear()
    load_gateway.cache_clear()
    load_sandbox.cache_clear()
    load_workspace.cache_clear()
    store.invalidate()
