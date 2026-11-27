"""配置加载 —— 强类型对象的唯一入口。

`config_store` 负责四层合流（代码默认 ← yaml 基线 ← 环境变量 ← config.db 覆盖），
本文件只把合流后的 dict 变成模型。policy / gates / model 仍是纯 yaml：
前两者分别属于「安全域知识」与「CI 构建期」，后者有自己的一套落盘实现。
"""

from __future__ import annotations

import re
from functools import cache
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# `as` 同名是显式再导出：旧代码与测试从 `config` 取这两个名字，改名只该改 import 源。
from devops_agent.config_files import CONFIG_DIR_ENV as CONFIG_DIR_ENV
from devops_agent.config_files import config_dir as config_dir
from devops_agent.config_files import read_yaml
from devops_agent.config_store import store
from devops_agent.memory_policy import MemoryPolicy

# 兼容旧名：既有代码与测试都通过 `config._load_yaml` / `config.config_dir` 取配置。
_load_yaml = read_yaml


# 权限（档位、工具集、builtin 规则）不在这里读配置：它是代码契约，声明在
# `governance/model.py`，编译在 `governance/compile.py`。同一份可用档位曾在
# policy.yaml 与代码里各写一遍，改一处忘另一处就会出现「前端列出的档位不是真正
# 开放的档位」——这类重复已随该次重构删除。

# 确认协议的默认值 —— 与 config/policy.yaml 的 confirm 段逐项对应。
# 文件缺失时（wheel 不含 config/）回落这里；两边一致由
# tests/unit/test_gateway_settings.py 的对账用例钉住，改一处就会红。
_DEFAULT_DANGER_PATTERNS: tuple[str, ...] = (
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f",
    r"rm\s+-[a-zA-Z]*f[a-zA-Z]*r",
    r":\s*\(\s*\)\s*\{",
    "mkfs",
    r"dd\s+if=",
    "shutdown",
    "reboot",
    r"chmod\s+-R\s+0?777",
    r">\s*/dev/sd",
)
_DEFAULT_WRITE_PATTERNS: tuple[str, ...] = (
    r"sed\s+-i",
    r">>?",
    r"\btouch\b",
    r"\bmkdir\b",
    r"\brm\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\btee\b",
)
_DEFAULT_SCOPES: tuple[str, ...] = ("once", "exact", "prefix")


class RiskPolicy(BaseModel):
    """风险分级规则：哪些命令算高危、哪些算写入（runtime/risk.py 的判定依据）。"""

    danger_patterns: list[str] = Field(default_factory=lambda: list(_DEFAULT_DANGER_PATTERNS))
    write_patterns: list[str] = Field(default_factory=lambda: list(_DEFAULT_WRITE_PATTERNS))

    @model_validator(mode="after")
    def _check_compilable(self) -> RiskPolicy:
        """正则必须在加载期就能编译：写错一条不该等到用户点「允许」时才炸。"""
        for name in ("danger_patterns", "write_patterns"):
            for pattern in getattr(self, name):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"confirm.risk.{name} 含非法正则 {pattern!r}: {exc}") from exc
        return self


class RulePolicy(BaseModel):
    """规则沉淀策略：把一次确认扩展成哪条 allow 规则（runtime/rules.py 的依据）。"""

    # 确认作用域，顺序即「由窄到宽」，前端按它渲染选项
    scopes: list[str] = Field(default_factory=lambda: list(_DEFAULT_SCOPES))
    # 需要看「子命令」的程序：前缀落到第二个词
    subcommand_programs: list[str] = Field(
        default_factory=lambda: [
            "git",
            "gh",
            "npm",
            "pnpm",
            "yarn",
            "npx",
            "pip",
            "uv",
            "poetry",
            "cargo",
            "go",
            "docker",
            "docker-compose",
            "kubectl",
            "helm",
            "terraform",
            "make",
            "dotnet",
            "mvn",
            "gradle",
            "apt",
            "apt-get",
            "brew",
            "systemctl",
            "journalctl",
            "az",
            "aws",
            "gcloud",
            "conda",
            "alembic",
        ]
    )
    # 复合命令分隔符：命中任意一个就不沉淀
    separators: list[str] = Field(default_factory=lambda: ["&&", "||", ";", "|", "\n"])
    # 包装层：先剥掉再判定真正的程序
    wrappers: list[str] = Field(
        default_factory=lambda: [
            "timeout",
            "time",
            "nice",
            "nohup",
            "stdbuf",
            "env",
            "command",
            "exec",
        ]
    )
    # 沉淀后等于开了任意执行的门，永不沉淀
    never_persist: list[str] = Field(
        default_factory=lambda: [
            "rm",
            "rmdir",
            "mkfs",
            "dd",
            "shutdown",
            "reboot",
            "chmod",
            "chown",
            "sudo",
            "su",
        ]
    )
    never_persist_interpreters: list[str] = Field(
        default_factory=lambda: ["bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"]
    )
    # 按「`-m` 后的模块」取前缀的程序
    python_programs: list[str] = Field(default_factory=lambda: ["python", "python3", "py"])

    @model_validator(mode="after")
    def _check_scopes(self) -> RulePolicy:
        """作用域必须非空且无重复：空列表让 parse_decisions 无处回落，重复项让前端选项重影。"""
        if not self.scopes:
            raise ValueError("confirm.rules.scopes 不能为空")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("confirm.rules.scopes 不得重复")
        return self


class ConfirmPolicy(BaseModel):
    """policy.yaml 的 confirm 段：确认协议的域知识（展示上限 + 风险 + 沉淀规则）。"""

    # 下发给前端的命令原文上限：只用于展示，超长由界面折叠，不影响真正执行的命令
    display_limit: int = Field(default=2000, ge=200, le=20_000)
    risk: RiskPolicy = Field(default_factory=RiskPolicy)
    rules: RulePolicy = Field(default_factory=RulePolicy)


class RuntimePolicy(BaseModel):
    """policy.yaml 的完整类型化视图（现在只剩确认协议）。

    模型参数（含 max_iters / 两级重试）已迁到 `config/model.yaml`（见
    `ModelSettings`）；权限档位与规则已迁到 `governance/model.py`。留多个入口会让
    同一件事有多个默认值 —— 前者造成过「两个 max_iters 打架」，后者造成过
    「yaml 里的 deny 规则从未命中却看起来在生效」。
    确认协议只带「域知识」（正则、程序分类、作用域），不带任何 HTTP 或 UI 决策。
    """

    confirm: ConfirmPolicy = Field(default_factory=ConfirmPolicy)


class GatewayStreamPolicy(BaseModel):
    """流式端点的策略旋钮（gateway.yaml 的 gateway.stream 段）。"""

    # 同一次 run 最多打扰用户几次确认
    ask_limit: int = Field(default=3, ge=1, le=20)
    # 挂起有效期（秒）：超时按「未确认」处理
    park_ttl_seconds: int = Field(default=600, ge=30, le=86_400)
    # 结清遗留挂起时最多追跑几轮
    settle_rounds: int = Field(default=2, ge=0, le=5)
    # SSE 静默补帧间隔（秒）
    heartbeat_seconds: float = Field(default=15.0, ge=1, le=120)
    # 确认次数读不到时的策略：reject=按超限自动拒绝 / park=照常弹卡片
    on_counter_unavailable: Literal["reject", "park"] = "reject"


class GatewaySessionsPolicy(BaseModel):
    """会话注册表策略（gateway.yaml 的 gateway.sessions 段）。"""

    max_entries: int = Field(default=16, ge=1, le=256)


class GatewayPolicy(BaseModel):
    """gateway.yaml 的完整类型化视图。

    只放部署策略；HTTP 状态码、帧名、DB 状态值一律不进来（见 gateway.yaml 的注释）。
    """

    stream: GatewayStreamPolicy = Field(default_factory=GatewayStreamPolicy)
    sessions: GatewaySessionsPolicy = Field(default_factory=GatewaySessionsPolicy)


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
        default_factory=lambda: {"human": 1.0, "judge": 0.8, "openjudge": 0.7, "rule": 0.6}
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


def load_health() -> HealthPolicy:
    """健康分口径：代码默认 ← health.yaml ← 环境变量 ← config.db 覆盖。

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
    """网关策略：代码默认 ← gateway.yaml ← 环境变量 ← config.db 覆盖。

    仍保留 @cache：装配期读一次就固定（`Settings.load()`），改完要重启进程 ——
    与其假装热更新，不如让界面明说「重启后生效」。
    """
    return GatewayPolicy.model_validate(store.effective("gateway"))


class InputPolicy(BaseModel):
    """输入管线的抓取与体积上限（input.yaml 的 input 段）。

    这些以前是 `input/pipeline/fetch.py` 里的模块常量。放进配置的理由与网关一致：
    超时与体积属于「部署环境」而不是「算法」—— 内网代理慢、抓大站点、CI 沙箱受限，
    每种环境想要的数不一样，改配置不该改代码。
    """

    # 单条远程抓取超时（秒）
    fetch_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # 单条远程响应体上限（字节）
    fetch_max_bytes: int = Field(default=512 * 1024, ge=4096, le=32 * 1024 * 1024)
    # 单个本地引用文件最多读取的字节数
    max_file_bytes: int = Field(default=256 * 1024, ge=4096, le=32 * 1024 * 1024)
    # 单条输入最多登记的引用条数
    max_references: int = Field(default=5, ge=1, le=50)
    # 重定向最多跟几跳（每一跳都会重新校验目标地址）
    max_redirects: int = Field(default=3, ge=0, le=10)
    # 是否允许抓取内网 / 回环 / 链路本地地址；默认拒绝（防服务端替人访问本地服务）
    allow_private_addresses: bool = False
    # 允许访问的端口：默认只放 80/443，避免把公网上的 Redis/ES 当网页抓
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])
    # 域名白/黑名单：空表示不限制（白名单非空时即「只允许这些」）
    allowed_hosts: list[str] = Field(default_factory=list)
    denied_hosts: list[str] = Field(default_factory=list)

    # --- 内容安全裁剪（SPEC-0012）-------------------------------------
    # 单条材料裁剪后的字符上限：超出按 head_ratio 保留头尾
    per_url_chars: int = Field(default=8000, ge=200, le=200_000)
    # 判「内容不足（thin）」的阈值：低于它就不再当完整资料
    min_chars: int = Field(default=400, ge=0, le=100_000)
    # 抓到多少字就认为「够用、可以提前停止」
    min_total_chars: int = Field(default=1200, ge=0, le=200_000)
    # 一轮材料总字符上限
    total_chars: int = Field(default=24_000, ge=1000, le=400_000)
    # 一轮预抓取的总时间预算（毫秒）
    total_ms: int = Field(default=8000, ge=500, le=120_000)
    # 裁剪时保留头部的比例，其余给尾部（网页结论常在尾部）
    head_ratio: float = Field(default=0.7, gt=0, le=1)
    # 是否扫描「疑似指令注入」段落（只标记，不删除）
    injection_scan: bool = True
    # 是否给网络材料加 <<untrusted-web>> 边界标记
    wrap_untrusted: bool = True


def load_input() -> InputPolicy:
    """输入管线策略：代码默认 ← input.yaml ← 环境变量 ← config.db 覆盖。

    每次抓取都读（存取层有 TTL 缓存），所以面板改完立即生效。
    """
    return InputPolicy.model_validate(store.effective("input"))


def load_memory() -> MemoryPolicy:
    """记忆策略：代码默认 ← memory.yaml ← 环境变量 ← config.db 覆盖。

    环境变量（enabled / backend / scope / user_id）由存取层的白名单合并，
    不再在这里二次覆盖 —— 两处都写会让优先级说不清。装配时读取，下一轮生效。
    """
    return MemoryPolicy.model_validate(store.effective("memory"))


def clear_cache() -> None:
    """测试中切换配置目录 / 库路径后调用。"""
    load_policy.cache_clear()
    load_gates.cache_clear()
    load_model.cache_clear()
    load_gateway.cache_clear()
    store.invalidate()
