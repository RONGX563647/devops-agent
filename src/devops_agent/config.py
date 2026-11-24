"""配置加载 —— config/*.yaml 的唯一读取入口，输出强类型对象。

路径定位优先级：
1. 环境变量 DEVOPS_AGENT_CONFIG_DIR（测试 / 部署覆盖）
2. 包位置推断的项目根 <root>/config（src/ 布局）

配置文件缺失时回退到内置默认值（wheel 不含 config/）。
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_DIR_ENV = "DEVOPS_AGENT_CONFIG_DIR"

PermissionModeName = Literal["default", "accept_edits", "explore", "bypass", "dont_ask"]
RuleBehaviorName = Literal["allow", "deny", "ask", "passthrough"]


def config_dir() -> Path:
    """返回配置文件目录，环境变量优先。"""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    path = config_dir() / name
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 顶层必须是映射")
    return cast(dict[str, Any], raw)


class RuleSpec(BaseModel):
    """一条权限规则的声明式描述。"""

    tool_name: str
    rule_content: str | None = None
    behavior: RuleBehaviorName = "deny"
    source: str = "policy"


class WorkingDirSpec(BaseModel):
    """一个附加工作目录的声明式描述。"""

    path: str
    source: str = "policy"


class PermissionPolicy(BaseModel):
    """policy.yaml 的 permission 段。"""

    # 无档位上下文时的回落模式（保留给脚本 / 单测直接建引擎的场景）
    mode: PermissionModeName = "default"
    # 前端默认选中的档位
    default_profile: str = "workspace"
    # 本部署开放的档位；留空表示用内置默认（不含 unrestricted）
    available: list[str] = Field(default_factory=list)
    # execute 档放行的命令前缀，写成 "pytest:*" 形式（框架按子命令逐个匹配）
    command_allowlist: list[str] = Field(default_factory=list)
    working_directories: list[WorkingDirSpec] = Field(default_factory=list)
    allow_rules: list[RuleSpec] = Field(default_factory=list)
    deny_rules: list[RuleSpec] = Field(default_factory=list)
    ask_rules: list[RuleSpec] = Field(default_factory=list)


class RuntimePolicy(BaseModel):
    """policy.yaml 的完整类型化视图。

    模型参数（含 max_iters / 两级重试）不在这里：它们已迁到 `config/model.yaml`
    （见 `ModelSettings`）。留两个入口会让同一件事有两个默认值。
    """

    permission: PermissionPolicy = Field(default_factory=PermissionPolicy)


class ModelSettings(BaseModel):
    """model.yaml 的 model 段：模型参数的部署默认。

    每项都带 `Field(ge=, le=)`：越界在加载期就报错，而不是等到某次模型调用 400。
    这里只放「部署者能决定」的值；`api_key` / `base_url` / `stream` 不在其中 ——
    凭据不该经 HTTP 往返，流式关掉会让前端退化成一次性渲染。
    """

    # 空串 = 用环境变量 DEVOPS_AGENT_MODEL
    name: str = ""
    # 上下文窗口：同时决定压缩阈值、圆环分母、压缩后保留量
    context_size: int = Field(default=128_000, ge=8192, le=2_000_000)
    # 部署者有没有确认过这个窗口值；false 时前端显示「未校准」
    context_calibrated: bool = False
    # 两级重试：最坏尝试 = (1 + model) × (1 + agent)
    model_max_retries: int = Field(default=3, ge=0, le=5)
    agent_max_retries: int = Field(default=3, ge=0, le=5)
    # 按域取值的迭代上限；未声明的域回落 default
    max_iters: dict[str, int] = Field(default_factory=lambda: {"default": 12})
    # 采样参数：None = 交给供应商默认（不猜）
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    reasoning_effort: str | None = None
    thinking_enable: bool | None = None
    parallel_tool_calls: bool = True
    # 上下文压缩（框架 ContextConfig 的三个比例）
    trigger_ratio: float = Field(default=0.8, gt=0, le=0.9)
    reserve_ratio: float = Field(default=0.1, gt=0, lt=0.9)
    context_buffer_ratio: float = Field(default=0.2, ge=0, le=1)

    def max_iters_for(self, domain: str) -> int:
        """按域取 max_iters，未声明的域回退 default。"""
        return int(self.max_iters.get(domain, self.max_iters.get("default", 12)))

    @model_validator(mode="after")
    def _check_ratios(self) -> ModelSettings:
        """联动约束在加载期就报错：框架的 `_validate_context_config_ratios` 会抛异常，
        与其等到某次推理前炸掉，不如启动时就说清哪个值写错了。"""
        if self.reserve_ratio >= self.trigger_ratio:
            raise ValueError("model.reserve_ratio 必须小于 model.trigger_ratio")
        return self


class QualityGates(BaseModel):
    """gates.yaml 的 quality_gates 段。"""

    coverage_fail_under: int = 80
    complexity_threshold: int = 10
    mutation_score: int = 70
    dead_code_min_confidence: int = 80


class HealthWeights(BaseModel):
    """健康分四类信号的权重（来自 health.yaml）。"""

    execution: float = 0.35
    efficiency: float = 0.25
    quality: float = 0.25
    safety: float = 0.15


class HealthPolicy(BaseModel):
    """health.yaml 的完整类型化视图（评分口径 v2）。

    v1 只有「权重 + 一个固定延迟基准」，长任务必然 0 分、缺耗时会白拿 100；
    v2 把这些都做成可配置的旋钮，口径写在配置里而不是散在代码常量里。
    """

    weights: HealthWeights = Field(default_factory=HealthWeights)
    # 第一个模型调用的时间预算；之后每次调用再叠加 per_step_baseline_ms。
    # 固定 5000ms 会把「5 步的正常长任务」判成 0 分，按步数给预算才可比。
    latency_baseline_ms: int = 5000
    per_step_baseline_ms: int = 6000
    # 超预算后的衰减曲线：score = 100 * (budget / actual) ** exponent。
    # 指数 < 1 表示「慢一点不至于零分」，但慢 3 倍仍明显掉分。
    latency_exponent: float = 0.6
    # 每次模型调用的 token 预算与衰减指数；会话越长单次上下文越大，这里按次均看
    token_baseline_per_call: int = 12_000
    token_exponent: float = 0.5
    # 效率子分里耗时与 token 的占比（某一项缺数据时按剩余权重重归一化）
    efficiency_split: dict[str, float] = Field(
        default_factory=lambda: {"latency": 0.7, "tokens": 0.3}
    )
    # 质量：不同评分来源的可信度，以及向先验收缩的强度
    # （prior_weight 相当于「先压一条中性样本」，避免单条 0 分把整轮判死）
    source_weights: dict[str, float] = Field(
        default_factory=lambda: {"human": 1.0, "judge": 0.8, "rule": 0.6}
    )
    quality_prior: float = 0.5
    quality_prior_weight: float = 1.0
    # 等级阈值：从高到低匹配，value >= 阈值即取该等级
    grades: dict[str, int] = Field(default_factory=lambda: {"A": 90, "B": 80, "C": 70, "D": 60})


@cache
def load_policy() -> RuntimePolicy:
    """加载并缓存运行时策略。"""
    return RuntimePolicy.model_validate(_load_yaml("policy.yaml"))


@cache
def load_gates() -> QualityGates:
    """加载并缓存 CI 质量阈值。"""
    data = _load_yaml("gates.yaml")
    return QualityGates.model_validate(data.get("quality_gates", data))


@cache
def load_health() -> HealthPolicy:
    """加载并缓存健康分配置（health.yaml 的 health 段）。"""
    data = _load_yaml("health.yaml")
    return HealthPolicy.model_validate(data.get("health", data))


@cache
def load_model() -> ModelSettings:
    """加载并缓存模型参数默认值（model.yaml 的 model 段）。

    文件缺失时返回内置默认（与 `load_policy` 同样的降级策略）：
    写错配置不该让服务起不来，只是全部回落到默认值。
    """
    data = _load_yaml("model.yaml")
    return ModelSettings.model_validate(data.get("model", data))


def clear_cache() -> None:
    """测试中切换配置目录后调用。"""
    load_policy.cache_clear()
    load_gates.cache_clear()
    load_health.cache_clear()
    load_model.cache_clear()
